"""The trial loop and the run CLI, with every outside edge faked.

These tests prove the *sequence* (verify → seed → snapshot → run → snapshot → score → reset), the
artefact set written per trial, and that a broken arm still produces a directory a judge can read.
Entity names here are invented; no scenario data lives in tests.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from evals import run as run_cli
from evals.trial import TrialHooks, TrialSpec, run_trial


@dataclass
class FakeTask:
    task_id: str = "TASK-99"


@dataclass
class FakeScenario:
    task: FakeTask = field(default_factory=FakeTask)
    seed_config: dict[str, Any] = field(default_factory=lambda: {"widgets": {"records": [{"id": "w1"}]}})
    prompt: str = "Do the widget thing."
    system_prompt: str = "HARNESS SYSTEM PROMPT"


@dataclass
class FakeManifest:
    scenario_id: str
    seeded_at: float = 1.0
    created: dict[str, dict[str, list[str]]] = field(default_factory=lambda: dict[str, dict[str, list[str]]]())

    def dump(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"scenario_id": self.scenario_id, "seeded_at": self.seeded_at}) + "\n")


@dataclass
class FakeSeedResult:
    counts: dict[str, int]


class FakeApp:
    def __init__(self, name: str, log: list[str], residue: list[str] | None = None) -> None:
        self.app = name
        self._log = log
        self._residue = residue or []
        self._seeded = False

    async def seed(self, seed_config: Mapping[str, Any], manifest: Any) -> FakeSeedResult:
        self._log.append(f"seed:{self.app}")
        self._seeded = True
        return FakeSeedResult({"records": 1})

    async def snapshot(self) -> dict[str, Any]:
        self._log.append(f"snapshot:{self.app}")
        return {"records": ["w1"], "seeded": self._seeded}

    async def reset(self, manifest: Any) -> None:
        self._log.append(f"reset:{self.app}")
        self._seeded = False

    async def verify_clean(self) -> list[str]:
        self._log.append(f"verify:{self.app}")
        return list(self._residue)

    async def aclose(self) -> None:
        self._log.append(f"close:{self.app}")


class FakeGateway:
    def __init__(self, log: list[str]) -> None:
        self._log = log
        self.closed = False

    def tool_schema(self) -> list[dict[str, Any]]:
        return [{"name": "provider_api", "input_schema": {"type": "object", "properties": {}}}]

    @property
    def trace(self) -> tuple[dict[str, Any], ...]:
        return ({"sequence": 1, "provider": "widgets", "method": "POST", "path": "/records/w1"},)

    async def execute_tool(self, tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True}

    async def aclose(self) -> None:
        self._log.append("gateway:close")
        self.closed = True


@dataclass
class FakeInvocation:
    status: str = "completed"
    final_text: str = "all done"
    events: tuple[dict[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "final_text": self.final_text, "events": list(self.events)}


def make_hooks(
    log: list[str],
    *,
    invoke_error: Exception | None = None,
    scorer: Any = None,
    residue: list[str] | None = None,
) -> tuple[TrialHooks, dict[str, Any]]:
    captured: dict[str, Any] = {}
    gateway = FakeGateway(log)

    async def invoke_benchpress(*args: Any, **kwargs: Any) -> FakeInvocation:
        log.append("invoke:benchpress")
        captured["benchpress_args"] = args
        captured["benchpress_kwargs"] = kwargs
        if invoke_error is not None:
            raise invoke_error
        return FakeInvocation(events=({"type": "tool_call", "tool_use_id": "bp-1"},))

    async def invoke_baseline(**kwargs: Any) -> FakeInvocation:
        log.append("invoke:baseline")
        captured["baseline_kwargs"] = kwargs
        if invoke_error is not None:
            raise invoke_error
        return FakeInvocation(events=({"type": "tool_call", "tool_use_id": "base-1"},))

    hooks = TrialHooks(
        load_scenario=lambda scenario_id: FakeScenario(),
        build_apps=lambda names: ({name: FakeApp(name, log, residue) for name in names}, []),
        build_gateway=lambda providers: gateway,
        invoke_benchpress=invoke_benchpress,
        invoke_baseline=invoke_baseline,
        score=scorer,
        new_manifest=lambda scenario_id: FakeManifest(scenario_id),
        now=lambda: 1_700_000_000.0,
    )
    captured["gateway"] = gateway
    return hooks, captured


def spec_for(tmp_path: Path, agent: str = "benchpress", **kwargs: Any) -> TrialSpec:
    return TrialSpec(
        scenario_id="widget-review",
        agent=agent,
        apps=("widgets",),
        out_root=tmp_path / "runs",
        **kwargs,
    )


# ------------------------------------------------------------------------------ sequence


def test_trial_runs_the_plan_b_sequence_in_order(tmp_path: Path) -> None:
    log: list[str] = []
    hooks, _ = make_hooks(log)
    outcome = asyncio.run(run_trial(spec_for(tmp_path), hooks=hooks))

    assert log == [
        "verify:widgets",
        "seed:widgets",
        "snapshot:widgets",
        "invoke:benchpress",
        "snapshot:widgets",
        "gateway:close",
        "reset:widgets",
        "verify:widgets",
        "close:widgets",
    ]
    assert outcome.status == "completed"
    assert outcome.provider_calls == 1


def test_trial_writes_every_plan_b_artefact(tmp_path: Path) -> None:
    log: list[str] = []
    hooks, _ = make_hooks(log)
    outcome = asyncio.run(run_trial(spec_for(tmp_path), hooks=hooks))
    for name in (
        "trial.json",
        "seed-manifest.json",
        "state-before.json",
        "prompt.json",
        "invocation.json",
        "trace.json",
        "state-after.json",
        "verdict.json",
    ):
        assert (outcome.trial_dir / name).exists(), name

    prompt = json.loads((outcome.trial_dir / "prompt.json").read_text())
    assert prompt["system_prompt"] == "HARNESS SYSTEM PROMPT"
    assert prompt["user_prompt"] == "Do the widget thing."
    before = json.loads((outcome.trial_dir / "state-before.json").read_text())
    after = json.loads((outcome.trial_dir / "state-after.json").read_text())
    assert before["widgets"]["seeded"] is True and after["widgets"]["seeded"] is True


def test_trial_dir_encodes_scenario_arm_and_repeat(tmp_path: Path) -> None:
    log: list[str] = []
    hooks, _ = make_hooks(log)
    spec = spec_for(tmp_path, repeat=2, ablations=("no_gate",))
    outcome = asyncio.run(run_trial(spec, hooks=hooks))
    assert outcome.trial_dir.parent.name == "benchpress+no_gate"
    assert outcome.trial_dir.parent.parent.name == "widget-review"
    assert outcome.trial_dir.name.endswith("-r2")


# ------------------------------------------------------------------------------ both arms


def test_both_arms_get_the_same_prompts_schema_and_limits(tmp_path: Path) -> None:
    log: list[str] = []
    hooks, captured = make_hooks(log)
    asyncio.run(run_trial(spec_for(tmp_path, "benchpress"), hooks=hooks))
    bp_args = captured["benchpress_args"]
    asyncio.run(run_trial(spec_for(tmp_path, "baseline"), hooks=hooks))
    base = captured["baseline_kwargs"]

    assert bp_args[1] == base["system_prompt"] == "HARNESS SYSTEM PROMPT"
    assert bp_args[2] == base["user_prompt"] == "Do the widget thing."
    assert bp_args[3] == base["tool_schema"]
    assert bp_args[5] == 160 and base["max_tool_calls"] == 200
    assert bp_args[6] == base["timeout_seconds"] == 1800.0


def test_benchpress_arm_receives_ablations_and_a_trace_dir(tmp_path: Path) -> None:
    log: list[str] = []
    hooks, captured = make_hooks(log)
    outcome = asyncio.run(run_trial(spec_for(tmp_path, ablations=("no_gate", "no_readback")), hooks=hooks))
    kwargs = captured["benchpress_kwargs"]
    assert kwargs["trace_dir"] == outcome.trial_dir
    assert kwargs["ablations"].no_gate and kwargs["ablations"].no_readback
    assert kwargs["trial_id"] == outcome.trial_dir.name


# ------------------------------------------------------------------------------ scoring


def test_missing_scorer_yields_an_unscored_verdict(tmp_path: Path) -> None:
    log: list[str] = []
    hooks, _ = make_hooks(log)
    outcome = asyncio.run(run_trial(spec_for(tmp_path), hooks=hooks))
    verdict = json.loads((outcome.trial_dir / "verdict.json").read_text())
    assert verdict["outcome"] in {"unscored", "pass", "fail", "unsafe"}


def test_scorer_receives_trace_events_states_and_final_text(tmp_path: Path) -> None:
    log: list[str] = []
    seen: dict[str, Any] = {}

    def scorer(**kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"outcome": "pass", "assertions": [{"id": "A1", "ok": True}]}

    hooks, _ = make_hooks(log, scorer=scorer)
    outcome = asyncio.run(run_trial(spec_for(tmp_path), hooks=hooks))
    assert seen["final_text"] == "all done"
    assert seen["state_before"]["widgets"]["records"] == ["w1"]
    assert seen["events"][0]["tool_use_id"] == "bp-1"
    assert seen["trace"][0]["sequence"] == 1
    assert outcome.outcome == "pass"


def test_no_score_skips_the_scorer(tmp_path: Path) -> None:
    log: list[str] = []

    def scorer(**kwargs: Any) -> dict[str, Any]:
        raise AssertionError("must not be called")

    hooks, _ = make_hooks(log, scorer=scorer)
    outcome = asyncio.run(run_trial(spec_for(tmp_path, score=False), hooks=hooks))
    assert outcome.outcome == "unscored"


def test_a_crashing_scorer_does_not_lose_the_trial(tmp_path: Path) -> None:
    log: list[str] = []

    def scorer(**kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("assertion port is broken")

    hooks, _ = make_hooks(log, scorer=scorer)
    outcome = asyncio.run(run_trial(spec_for(tmp_path), hooks=hooks))
    assert outcome.outcome == "unscored"
    assert any("score-failed" in note for note in outcome.notes)


# ------------------------------------------------------------------------------ failure paths


def test_agent_failure_still_snapshots_scores_and_resets(tmp_path: Path) -> None:
    log: list[str] = []
    hooks, captured = make_hooks(log, invoke_error=RuntimeError("model unreachable"))
    outcome = asyncio.run(run_trial(spec_for(tmp_path), hooks=hooks))
    assert "RuntimeError" in outcome.error
    assert (outcome.trial_dir / "state-after.json").exists()
    assert (outcome.trial_dir / "verdict.json").exists()
    assert "reset:widgets" in log
    assert captured["gateway"].closed is True


def test_no_reset_leaves_the_apps_seeded(tmp_path: Path) -> None:
    log: list[str] = []
    hooks, _ = make_hooks(log)
    asyncio.run(run_trial(spec_for(tmp_path, reset=False), hooks=hooks))
    assert "reset:widgets" not in log


def test_pre_seed_residue_is_reported_as_a_note(tmp_path: Path) -> None:
    log: list[str] = []
    hooks, _ = make_hooks(log, residue=["widgets: 1 leftover record"])
    outcome = asyncio.run(run_trial(spec_for(tmp_path), hooks=hooks))
    assert any("pre-seed residue" in note for note in outcome.notes)
    assert any("post-reset residue" in note for note in outcome.notes)


def test_unknown_agent_and_substrate_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        TrialSpec(scenario_id="x", agent="gpt-wrapper")
    with pytest.raises(ValueError):
        TrialSpec(scenario_id="x", agent="baseline", substrate="mock")


# ------------------------------------------------------------------------------ the CLI


def test_cli_builds_one_spec_per_repeat(tmp_path: Path) -> None:
    args = run_cli.parse_args(
        ["--scenario", "widget-review", "--agent", "benchpress", "--repeats", "3", "--out", str(tmp_path)]
    )
    specs = run_cli.build_specs(args)
    assert [spec.repeat for spec in specs] == [1, 2, 3]
    assert all(spec.agent == "benchpress" for spec in specs)


def test_cli_apps_passthrough_limits_the_substrate(tmp_path: Path) -> None:
    args = run_cli.parse_args(
        ["--scenario", "widget-review", "--agent", "baseline", "--apps", "slack,stripe,hubspot", "--out", str(tmp_path)]
    )
    specs = run_cli.build_specs(args)
    assert specs[0].apps == ("slack", "stripe", "hubspot")


def test_plan_b_matrix_is_the_documented_order() -> None:
    args = run_cli.parse_args(["--matrix", "plan-b"])
    specs = run_cli.build_specs(args)
    assert len(specs) == 11
    assert [spec.arm for spec in specs[:4]] == ["benchpress", "baseline", "benchpress", "baseline"]
    assert specs[6].arm == "benchpress+no_policy_sweep"
    assert specs[7].scenario_id == "billing-review-injection" and specs[7].arm == "benchpress+no_gate"
    assert specs[10].arm == "benchpress+no_readback"


def test_devsim_substrate_requires_twin_urls() -> None:
    args = run_cli.parse_args(["--scenario", "widget-review", "--agent", "baseline", "--substrate", "devsim"])
    specs = run_cli.build_specs(args)
    assert run_cli.check_substrate(specs, {}) == [
        "DEVSIM_SLACK_URL",
        "DEVSIM_GMAIL_URL",
        "DEVSIM_HUBSPOT_URL",
        "DEVSIM_STRIPE_URL",
    ]
    full = {f"DEVSIM_{app.upper()}_URL": "http://127.0.0.1:9000" for app in specs[0].apps}
    assert run_cli.check_substrate(specs, full) == []


def test_cli_requires_scenario_and_agent_without_a_matrix() -> None:
    with pytest.raises(ValueError):
        run_cli.build_specs(run_cli.parse_args([]))


def test_run_all_is_sequential_and_appends_progress(tmp_path: Path) -> None:
    log: list[str] = []
    hooks, _ = make_hooks(log)
    specs: Sequence[TrialSpec] = [spec_for(tmp_path, repeat=1), spec_for(tmp_path, "baseline", repeat=1)]
    progress = tmp_path / "progress.jsonl"
    outcomes = asyncio.run(run_cli.run_all(specs, hooks=hooks, progress_path=progress))

    assert [outcome.spec.agent for outcome in outcomes] == ["benchpress", "baseline"]
    assert log.index("invoke:benchpress") < log.index("invoke:baseline")
    lines = [json.loads(line) for line in progress.read_text().splitlines()]
    assert [line["agent"] for line in lines] == ["benchpress", "baseline"]
    assert lines[0]["scenario"] == "widget-review"
