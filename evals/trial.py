"""One trial: verify clean → seed → snapshot → run → snapshot → score → reset.

A *trial* is the atomic unit of the Plan B rehearsal eval. It is deliberately the same
sequence for both arms — only `spec.agent` selects the scaffold — so the comparison is about the
scaffold and nothing else. Every side effect is written to one directory so a judge can reconstruct
the run from files alone:

```
runs/<scenario>/<agent>[+<ablations>]/<ts>-r<n>/
  trial.json          # this trial's identity: scenario, agent, ablations, model, substrate, apps
  seed-manifest.json  # exactly what the seeders created, so reset can remove exactly that
  state-before.json   # per-app snapshot taken after seeding, before the agent ran
  prompt.json         # the harness system prompt + suite user prompt, verbatim, as sent
  invocation.json     # harness-shaped invocation record (events, usage, config, status)
  trace.json          # the gateway's own trace records (fingerprints, sequence, status codes)
  state-after.json    # per-app snapshot taken after the agent finished, before reset
  verdict.json        # evals.assertions.score(...), or {"outcome": "unscored"} when absent
  receipt.json        # Benchpress only: the auditable Context dump
  benchpress-trace.jsonl
```

Injection points (`TrialHooks`) exist so the whole loop is testable without a network, a model key
or a scratch account; the defaults are the real thing.
"""

from __future__ import annotations

import importlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNS = REPO_ROOT / "runs"
DEFAULT_APPS: tuple[str, ...] = ("slack", "gmail", "hubspot", "stripe")
KNOWN_AGENTS: tuple[str, ...] = ("benchpress", "baseline")
MAX_PROVIDER_CALLS = 160
MAX_DOCS_CALLS = 40
MAX_WALL_SECONDS = 1_800.0
UNSCORED: dict[str, Any] = {"outcome": "unscored", "assertions": []}


# --------------------------------------------------------------------------------------
# Protocols (kept structural so tests can pass plain fakes)
# --------------------------------------------------------------------------------------


class GatewayLike(Protocol):
    def tool_schema(self) -> list[dict[str, Any]]: ...

    @property
    def trace(self) -> tuple[dict[str, Any], ...]: ...

    async def execute_tool(self, tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]: ...

    async def aclose(self) -> None: ...


class AppLike(Protocol):
    async def seed(self, seed_config: Mapping[str, Any], manifest: Any) -> Any: ...

    async def snapshot(self) -> dict[str, Any]: ...

    async def reset(self, manifest: Any) -> None: ...

    async def verify_clean(self) -> list[str]: ...

    async def aclose(self) -> None: ...


class InvocationLike(Protocol):
    status: str
    final_text: str
    events: tuple[dict[str, Any], ...]

    def as_dict(self) -> dict[str, Any]: ...


# --------------------------------------------------------------------------------------
# Spec and result
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TrialSpec:
    scenario_id: str
    agent: str
    repeat: int = 1
    apps: tuple[str, ...] = DEFAULT_APPS
    ablations: tuple[str, ...] = ()
    model: str | None = None
    substrate: str = "real"
    score: bool = True
    reset: bool = True
    out_root: Path = DEFAULT_RUNS

    def __post_init__(self) -> None:
        if self.agent not in KNOWN_AGENTS:
            raise ValueError(f"unknown agent {self.agent!r}; known: {', '.join(KNOWN_AGENTS)}")
        if self.substrate not in {"real", "devsim"}:
            raise ValueError(f"unknown substrate {self.substrate!r}; known: real, devsim")

    @property
    def arm(self) -> str:
        """`baseline`, `benchpress`, or `benchpress+no_gate` — the comparison axis label."""
        return self.agent + ("+" + ",".join(self.ablations) if self.ablations else "")

    def trial_dir(self, *, now: float | None = None) -> Path:
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(now if now is not None else time.time()))
        return self.out_root / self.scenario_id / self.arm / f"{stamp}-r{self.repeat}"


@dataclass(frozen=True)
class TrialOutcome:
    spec: TrialSpec
    trial_dir: Path
    outcome: str
    status: str
    elapsed_s: float
    provider_calls: int
    error: str = ""
    notes: tuple[str, ...] = ()

    def summary(self) -> dict[str, Any]:
        return {
            "scenario": self.spec.scenario_id,
            "agent": self.spec.agent,
            "arm": self.spec.arm,
            "ablations": list(self.spec.ablations),
            "repeat": self.spec.repeat,
            "substrate": self.spec.substrate,
            "trial_dir": str(self.trial_dir),
            "outcome": self.outcome,
            "status": self.status,
            "elapsed_s": round(self.elapsed_s, 2),
            "provider_calls": self.provider_calls,
            "error": self.error,
            "notes": list(self.notes),
        }


# --------------------------------------------------------------------------------------
# Hooks
# --------------------------------------------------------------------------------------


@dataclass
class TrialHooks:
    """Every outside-world edge of a trial, injectable. `None` means "use the real one"."""

    load_scenario: Callable[[str], Any] | None = None
    build_apps: Callable[[Sequence[str]], tuple[dict[str, AppLike], list[str]]] | None = None
    build_gateway: Callable[[Sequence[str]], GatewayLike] | None = None
    invoke_benchpress: Callable[..., Any] | None = None
    invoke_baseline: Callable[..., Any] | None = None
    score: Callable[..., dict[str, Any]] | None = None
    new_manifest: Callable[[str], Any] | None = None
    now: Callable[[], float] = time.time


def default_load_scenario(scenario_id: str) -> Any:
    return importlib.import_module("evals.scenarios").load(scenario_id)


def default_build_apps(names: Sequence[str]) -> tuple[dict[str, AppLike], list[str]]:
    import os

    seed = importlib.import_module("evals.seed")
    apps, skipped = cast(
        tuple[dict[str, AppLike], list[str]],
        seed.build_apps(list(names), dict(os.environ)),
    )
    return apps, skipped


def default_new_manifest(scenario_id: str) -> Any:
    from evals.realapps.base import SeedManifest

    return SeedManifest(scenario_id=scenario_id)


def default_build_gateway(providers: Sequence[str]) -> GatewayLike:
    from benchpress.realapp import gateway_from_env

    bridge = importlib.import_module("evals.harness_bridge")
    docs = bridge.docs_executor(list(providers))
    return cast(GatewayLike, gateway_from_env(list(providers), docs_executor=docs))


def load_scorer() -> Callable[..., dict[str, Any]] | None:
    """`evals.assertions.score` if a sibling has landed it; otherwise None (verdict `unscored`)."""
    try:
        module = importlib.import_module("evals.assertions")
    except ImportError:
        return None
    scorer = getattr(module, "score", None)
    if scorer is None:
        return None

    def call(**kwargs: Any) -> dict[str, Any]:
        verdict = cast(Callable[..., Any], scorer)(kwargs.pop("task"), **kwargs)
        to_dict = getattr(verdict, "to_dict", None)
        if callable(to_dict):
            return cast(dict[str, Any], cast(Callable[[], Any], to_dict)())
        return cast(dict[str, Any], verdict)

    return call


# --------------------------------------------------------------------------------------
# The trial
# --------------------------------------------------------------------------------------


async def run_trial(spec: TrialSpec, *, hooks: TrialHooks | None = None) -> TrialOutcome:
    """Run one trial end to end. Never raises for an agent failure: it writes what it has."""
    hook = hooks or TrialHooks()
    load_scenario = hook.load_scenario or default_load_scenario
    build_apps = hook.build_apps or default_build_apps
    build_gateway = hook.build_gateway or default_build_gateway
    new_manifest = hook.new_manifest or default_new_manifest

    loaded = load_scenario(spec.scenario_id)
    task = loaded.task
    seed_config: dict[str, Any] = dict(loaded.seed_config)
    providers = tuple(spec.apps)

    trial_dir = spec.trial_dir(now=hook.now())
    trial_dir.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []
    started = time.monotonic()

    apps, skipped = build_apps(providers)
    notes.extend(f"skipped:{entry}" for entry in skipped)
    _write(
        trial_dir / "trial.json",
        {
            "protocol": "benchpress-trial/1",
            "scenario": spec.scenario_id,
            "task_id": task.task_id,
            "agent": spec.agent,
            "arm": spec.arm,
            "ablations": list(spec.ablations),
            "repeat": spec.repeat,
            "apps": list(providers),
            "seeded_apps": sorted(apps),
            "substrate": spec.substrate,
            "model": spec.model or "",
            "started_at": hook.now(),
            "limits": {
                "max_provider_calls": MAX_PROVIDER_CALLS,
                "max_docs_calls": MAX_DOCS_CALLS,
                "timeout_seconds": MAX_WALL_SECONDS,
            },
        },
    )
    _write(
        trial_dir / "prompt.json",
        {
            "scenario": spec.scenario_id,
            "task_id": task.task_id,
            "system_prompt": loaded.system_prompt,
            "user_prompt": loaded.prompt,
            "providers": list(providers),
            "source": "evals.harness_bridge (harness SYSTEM_PROMPT + suite prompt, verbatim)",
        },
    )

    manifest = new_manifest(spec.scenario_id)
    gateway: GatewayLike | None = None
    invocation_dict: dict[str, Any] = {}
    status = "not_run"
    error = ""
    provider_calls = 0

    try:
        residue: list[str] = []
        for app in apps.values():
            residue.extend(await app.verify_clean())
        if residue:
            notes.extend(f"pre-seed residue:{item}" for item in residue)

        for name, app in apps.items():
            result = await app.seed(seed_config, manifest)
            notes.append(f"seeded:{name} {dict(result.counts)}")
        _dump_manifest(manifest, trial_dir / "seed-manifest.json")
        _write(trial_dir / "state-before.json", await _snapshot(apps))

        gateway = build_gateway(providers)
        tool_schema = gateway.tool_schema()
        invocation = await _invoke(
            spec,
            hook,
            loaded=loaded,
            tool_schema=tool_schema,
            gateway=gateway,
            trial_dir=trial_dir,
        )
        invocation_dict = invocation.as_dict()
        status = str(invocation_dict.get("status", "unknown"))
        provider_calls = len(gateway.trace)
        _write(trial_dir / "invocation.json", invocation_dict)
        _write(trial_dir / "trace.json", {"protocol": "benchpress-trace/1", "events": list(gateway.trace)})
        _write(trial_dir / "state-after.json", await _snapshot(apps))
    except Exception as exc:  # noqa: BLE001 - a broken trial is data, not a crash
        error = f"{type(exc).__name__}: {exc}"[:500]
        status = status if status != "not_run" else "trial_error"
        notes.append(f"error:{error}")
        if not (trial_dir / "state-after.json").exists():
            _write(trial_dir / "state-after.json", await _snapshot(apps))
    finally:
        if gateway is not None:
            await gateway.aclose()

    verdict = _score(
        spec,
        hook,
        task=task,
        trial_dir=trial_dir,
        invocation=invocation_dict,
        notes=notes,
    )
    _write(trial_dir / "verdict.json", verdict)

    if spec.reset:
        for name, app in apps.items():
            try:
                await app.reset(manifest)
            except Exception as exc:  # noqa: BLE001 - report, never mask, a failed reset
                notes.append(f"reset-failed:{name}:{type(exc).__name__}: {exc}"[:300])
        residue = []
        for app in apps.values():
            residue.extend(await app.verify_clean())
        notes.extend(f"post-reset residue:{item}" for item in residue)
    for app in apps.values():
        await app.aclose()

    return TrialOutcome(
        spec=spec,
        trial_dir=trial_dir,
        outcome=str(verdict.get("outcome", "unscored")),
        status=status,
        elapsed_s=time.monotonic() - started,
        provider_calls=provider_calls,
        error=error,
        notes=tuple(notes),
    )


async def _invoke(
    spec: TrialSpec,
    hook: TrialHooks,
    *,
    loaded: Any,
    tool_schema: list[dict[str, Any]],
    gateway: GatewayLike,
    trial_dir: Path,
) -> InvocationLike:
    model_id = spec.model or _default_model()
    if spec.agent == "baseline":
        invoke = hook.invoke_baseline
        if invoke is None:
            from evals.baseline import invoke_baseline

            invoke = invoke_baseline
        return cast(
            InvocationLike,
            await invoke(
                model_id=model_id,
                system_prompt=loaded.system_prompt,
                user_prompt=loaded.prompt,
                tool_schema=tool_schema,
                execute_tool=gateway.execute_tool,
                max_tool_calls=MAX_PROVIDER_CALLS + MAX_DOCS_CALLS,
                timeout_seconds=MAX_WALL_SECONDS,
            ),
        )

    invoke = hook.invoke_benchpress
    if invoke is None:
        from benchpress.adapter import invoke as invoke_benchpress

        invoke = invoke_benchpress
    from benchpress.phases.common import Ablations

    return cast(
        InvocationLike,
        await invoke(
            model_id,
            loaded.system_prompt,
            loaded.prompt,
            tool_schema,
            gateway.execute_tool,
            MAX_PROVIDER_CALLS,
            MAX_WALL_SECONDS,
            ablations=Ablations.parse(",".join(spec.ablations)),
            trace_dir=trial_dir,
            trial_id=trial_dir.name,
        ),
    )


def _score(
    spec: TrialSpec,
    hook: TrialHooks,
    *,
    task: Any,
    trial_dir: Path,
    invocation: Mapping[str, Any],
    notes: list[str],
) -> dict[str, Any]:
    if not spec.score:
        return dict(UNSCORED) | {"reason": "--no-score"}
    scorer = hook.score or load_scorer()
    if scorer is None:
        notes.append("unscored:evals.assertions.score is not available")
        return dict(UNSCORED) | {"reason": "evals.assertions.score unavailable"}
    try:
        verdict = scorer(
            task=task,
            trace=_read(trial_dir / "trace.json").get("events", []),
            events=list(invocation.get("events", [])),
            state_before=_read(trial_dir / "state-before.json"),
            state_after=_read(trial_dir / "state-after.json"),
            final_text=str(invocation.get("final_text", "")),
        )
    except Exception as exc:  # noqa: BLE001 - a scorer crash must not lose the trial
        notes.append(f"score-failed:{type(exc).__name__}: {exc}"[:300])
        return dict(UNSCORED) | {"reason": f"scorer raised {type(exc).__name__}"}
    return dict(verdict)


async def _snapshot(apps: Mapping[str, AppLike]) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    for name, app in apps.items():
        try:
            snapshot[name] = await app.snapshot()
        except Exception as exc:  # noqa: BLE001 - a missing snapshot is recorded, not fatal
            snapshot[name] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    return snapshot


def _default_model() -> str:
    import os

    return os.environ.get("BENCHPRESS_MODEL", "deepseek-v4-pro")


def _dump_manifest(manifest: Any, path: Path) -> None:
    dump = getattr(manifest, "dump", None)
    if callable(dump):
        cast(Callable[[Path], None], dump)(path)
        return
    _write(path, cast(dict[str, Any], getattr(manifest, "__dict__", {})))


def _write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n")


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data: object = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}
    return cast(dict[str, Any], data) if isinstance(data, dict) else {}


__all__ = [
    "DEFAULT_APPS",
    "DEFAULT_RUNS",
    "KNOWN_AGENTS",
    "TrialHooks",
    "TrialOutcome",
    "TrialSpec",
    "load_scorer",
    "run_trial",
]
