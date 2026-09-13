"""Drive the UNMODIFIED `scripts/run_argabench_40.py:run_task` against local twins.

The script is loaded with importlib (the pattern in the harness's own
`tests/test_argabench_resume.py:14–18`) and exactly these module globals are swapped:

- `SubprocessArgaCli` → a zero-arg factory returning the shared `FakeArgaCli`. `run_task`
  constructs `SubprocessArgaCli()` per trial (line 814) and `resolve_scenarios` uses
  `async with SubprocessArgaCli()` (line 758); both now hit local twins.
- `invoke_model` → the candidate callable (line 901). This is the only seam for the model.
- `load_profile` → `devsim.matrix.load_profile` (optional; only `async_main` calls it, which we
  never run because it requires `ARGA_API_KEY`). Swapped so the module is consistent if anything
  else reaches for it.

NOT swapped, deliberately: `wait_ready` and `wait_cleanup`, although they are module globals too.
Both are protocol-driven: `FakeArgaCli.create_twin_run` returns `status == "ready"` so
`wait_ready` returns without polling (line 662), and `teardown` leaves the run terminal with
`twins == {}` so `wait_cleanup` returns on its first `status` poll (line 710). Keeping the harness's
own functions means `cleanup.json` is produced by unmodified code. `resolve_scenarios` is also
kept: it calls `list_scenarios(tag="suite:argabench-40-v1")` and matches the 40 results by their
`content-sha256:` tag and description, which the repo's scenario files satisfy.

Everything else in the trial — `ProviderGateway`, `OfficialDocsGateway`, `TrustedStateCapturer`,
`diff_trusted_states`, `trace_artifacts`, `write_private_json`, `estimated_cost` — is the
harness's own code, untouched.

Output layout (what `reporting/argabench_matrix.py:classify_argabench_matrix` walks):

    <output>/matrix-config.json                      argabench-model-matrix-run/1 (37 profiles)
    <output>/model-matrix.json                       37-profile copy with devsim slots swapped in
    <output>/profiles/<profile_id>/run-config.json   argabench-run/2
    <output>/profiles/<profile_id>/staging-scenarios.json
    <output>/profiles/<profile_id>/tasks/<TASK-ID>/  the 11 trial artifacts written by run_task
    <output>/tasks -> profiles/<profile_id>/tasks    convenience symlink (most recent profile)
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any, cast

from devsim.candidates import Candidate
from devsim.harness import (
    RUN_SCRIPT_RELATIVE,
    SUITE_ID,
    SUITE_TAG,
    content_hash,
    ensure_importable,
    harness_module,
    load_suite,
    read_json_object,
    suite_task,
)
from devsim.lifecycle import SUBSTRATE, FakeArgaCli, scenario_content_digest, scenario_id_for
from devsim.matrix import (
    EXPECTED_PROFILE_COUNT,
    PROFILE_IDENTITY_FIELDS,
    canonical_profiles,
    load_profile,
    matrix_with_profiles,
    validate_profile,
    write_matrix,
)

MODULE_NAME = "devsim_argabench_40_runner"
DEVSIM_ENVIRONMENT = "devsim://127.0.0.1"
MATRIX_CONFIG_PROTOCOL = "argabench-model-matrix-run/1"
RUN_CONFIG_PROTOCOL = "argabench-run/2"
LIFECYCLE_CONCURRENCY = 3
CLEANUP_CONCURRENCY = 3
SWAPPED_GLOBALS: tuple[str, ...] = ("SubprocessArgaCli", "invoke_model", "load_profile")
TRIAL_ARTIFACTS: tuple[str, ...] = (
    "attempt.json",
    "control.json",
    "prompt.json",
    "baseline-state.json",
    "invocation.json",
    "provider-trace.json",
    "official-docs-trace.json",
    "tool-steps.json",
    "final-state.json",
    "raw-state-diff.json",
    "cleanup.json",
)

_MODULES: dict[Path, ModuleType] = {}


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


# --------------------------------------------------------------------------------------
# Loading and patching the harness runner
# --------------------------------------------------------------------------------------


def load_run_script(root: Path | None = None) -> ModuleType:
    """Import `<root>/scripts/run_argabench_40.py` as a module (cached per path)."""

    resolved = ensure_importable(root)
    path = resolved / RUN_SCRIPT_RELATIVE
    cached = _MODULES.get(path)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(MODULE_NAME, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    _MODULES[path] = module
    return module


@dataclass
class PatchedGlobals:
    module: ModuleType
    previous: dict[str, Any]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self.previous)

    def restore(self) -> None:
        for name, value in self.previous.items():
            setattr(self.module, name, value)
        self.previous = {}


def patch_run_script(
    module: ModuleType,
    *,
    cli: FakeArgaCli,
    invoke_model: Candidate,
    profile_loader: Callable[[str], dict[str, Any]] = load_profile,
) -> PatchedGlobals:
    """Swap the three globals listed in the module docstring; `restore()` undoes it."""

    for name in SWAPPED_GLOBALS:
        if not hasattr(module, name):
            raise AttributeError(f"run_argabench_40 has no global {name!r}; the harness contract changed")
    previous: dict[str, Any] = {name: getattr(module, name) for name in SWAPPED_GLOBALS}

    def cli_factory(*_args: object, **_kwargs: object) -> FakeArgaCli:
        return cli

    setattr(module, "SubprocessArgaCli", cli_factory)  # noqa: B010 - dynamic module globals
    setattr(module, "invoke_model", invoke_model)  # noqa: B010
    setattr(module, "load_profile", profile_loader)  # noqa: B010
    return PatchedGlobals(module=module, previous=previous)


# --------------------------------------------------------------------------------------
# Output root: matrix-config / run-config / staging-scenarios
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PreparedOutput:
    matrix_dir: Path
    profile_dir: Path
    matrix_config_path: Path
    model_matrix_path: Path
    run_config_path: Path
    staging_scenarios_path: Path
    task_ids: tuple[str, ...]


def default_scenario_ids(tasks: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Scenario ids as `FakeArgaCli.list_scenarios` would report them (no servers needed)."""

    catalog = FakeArgaCli().catalog
    by_digest = {scenario_content_digest(item): item for item in catalog}
    resolved: dict[str, str] = {}
    for task in tasks:
        task_digest = content_hash(dict(task))
        scenario = by_digest.get(task_digest)
        resolved[str(task["id"])] = scenario_id_for(scenario) if scenario is not None else f"devsim-{task_digest[:16]}"
    return resolved


def _write_private_json(path: Path, payload: Mapping[str, Any]) -> None:
    harness_module("arga_twins_benchmark.lifecycle").write_private_json(path, dict(payload))


def _suite_task_list() -> list[dict[str, Any]]:
    return [cast(dict[str, Any], task) for task in cast(list[object], load_suite()["tasks"]) if isinstance(task, dict)]


def _ordered_task_ids(*groups: Sequence[str]) -> list[str]:
    wanted = {task_id for group in groups for task_id in group}
    ordered = [str(task["id"]) for task in _suite_task_list()]
    unknown = sorted(wanted - set(ordered))
    if unknown:
        raise KeyError(f"unknown ArgaBench task ids: {', '.join(unknown)}")
    return [task_id for task_id in ordered if task_id in wanted]


def _identity(profile: Mapping[str, Any]) -> dict[str, Any]:
    return {name: profile.get(name) for name in PROFILE_IDENTITY_FIELDS}


def _existing_devsim_profiles(matrix_config: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if matrix_config is None:
        return []
    canonical_ids = {str(profile["id"]) for profile in canonical_profiles()}
    raw = matrix_config.get("profiles")
    if not isinstance(raw, list):
        return []
    return [
        cast(dict[str, Any], item)
        for item in cast(list[object], raw)
        if isinstance(item, dict) and str(cast(dict[str, Any], item).get("id")) not in canonical_ids
    ]


def _point_tasks_symlink(matrix_dir: Path, profile_id: str) -> None:
    link = matrix_dir / "tasks"
    target = Path("profiles") / profile_id / "tasks"
    if link.exists() and not link.is_symlink():
        return  # a real directory: never touch it
    temporary = matrix_dir / f".tasks.{os.getpid()}.tmp"
    if temporary.is_symlink() or temporary.exists():
        temporary.unlink()
    os.symlink(target, temporary, target_is_directory=True)
    os.replace(temporary, link)


def prepare_output_root(
    output: Path,
    profile: Mapping[str, Any],
    task_ids: Sequence[str],
    *,
    scenario_ids: Mapping[str, str] | None = None,
    concurrency: int = 1,
    environment: str = DEVSIM_ENVIRONMENT,
    module: ModuleType | None = None,
) -> PreparedOutput:
    """Write the matrix/profile config files the classifier requires; idempotent and additive.

    Re-running for another profile or task in the same `output` merges: the 37-profile copy gains
    the new devsim profile (one more canonical slot swapped), `task_ids` become the union.
    """

    if not 1 <= concurrency <= 16:
        raise ValueError("concurrency must be 1–16 (argabench_semantic_report.py:1686 scoring_ready)")
    runner = module or load_run_script()
    validated = validate_profile(profile)
    profile_id = str(validated["id"])
    matrix_dir = Path(output).resolve()
    matrix_dir.mkdir(parents=True, exist_ok=True)
    profile_dir = matrix_dir / "profiles" / profile_id
    (profile_dir / "tasks").mkdir(parents=True, exist_ok=True)

    matrix_config_path = matrix_dir / "matrix-config.json"
    existing_config = read_json_object(matrix_config_path) if matrix_config_path.is_file() else None
    existing_task_ids = cast(list[str], existing_config.get("task_ids", [])) if existing_config else []
    all_task_ids = _ordered_task_ids(existing_task_ids, list(task_ids))

    devsim_profiles = _existing_devsim_profiles(existing_config)
    known = next((item for item in devsim_profiles if str(item.get("id")) == profile_id), None)
    canonical_ids = {str(item["id"]) for item in canonical_profiles()}
    if known is not None and _identity(known) != _identity(validated):
        raise ValueError(f"{matrix_config_path} already has profile {profile_id!r} with a different identity")
    if known is None and profile_id not in canonical_ids:
        devsim_profiles.append(validated)
    matrix = matrix_with_profiles(devsim_profiles)
    model_matrix_path = write_matrix(matrix_dir / "model-matrix.json", matrix)

    matrix_config: dict[str, Any] = {
        "protocol": MATRIX_CONFIG_PROTOCOL,
        "suite_id": SUITE_ID,
        "profiles": matrix["profiles"],
        "profile_count": EXPECTED_PROFILE_COUNT,
        "task_ids": all_task_ids,
        "scenarios_per_profile": len(all_task_ids),
        "total_trials": EXPECTED_PROFILE_COUNT * len(all_task_ids),
        "global_trial_concurrency": concurrency,
        "per_profile_trial_concurrency": concurrency,
        "per_profile_lifecycle_concurrency": LIFECYCLE_CONCURRENCY,
        "per_profile_cleanup_concurrency": CLEANUP_CONCURRENCY,
        "profile_launch_interval_seconds": 0,
        "attempts_per_model_scenario_pair": 1,
        "google_profile_concurrency": 1,
        "google_task_concurrency": 1,
        "environment": environment,
        "started_at": (existing_config or {}).get("started_at") or utc_now(),
        "devsim": {
            "substrate": SUBSTRATE,
            "model_matrix": model_matrix_path.name,
            "devsim_profile_ids": [str(item["id"]) for item in devsim_profiles],
            "note": "Local twins; lifecycle artifacts come from devsim.lifecycle.FakeArgaCli. Not Arga twins.",
        },
    }
    _write_private_json(matrix_config_path, matrix_config)

    suite = load_suite()
    run_config_path = profile_dir / "run-config.json"
    existing_run_config = read_json_object(run_config_path) if run_config_path.is_file() else None
    profile_task_ids = _ordered_task_ids(
        cast(list[str], existing_run_config.get("task_ids", [])) if existing_run_config else [],
        list(task_ids),
    )
    profile_tasks: list[dict[str, Any]] = [
        task for task in _suite_task_list() if str(task.get("id")) in set(profile_task_ids)
    ]
    run_config = cast(
        dict[str, Any],
        runner._run_config_payload(suite=suite, profile=dict(validated), concurrency=concurrency, tasks=profile_tasks),  # noqa: SLF001
    )
    run_config["environment"] = environment
    if existing_run_config is not None and existing_run_config.get("started_at"):
        run_config["started_at"] = existing_run_config["started_at"]
    _write_private_json(run_config_path, run_config)

    staging_path = profile_dir / "staging-scenarios.json"
    existing_staging = read_json_object(staging_path) if staging_path.is_file() else None
    merged_ids: dict[str, str] = {}
    if existing_staging is not None:
        raw_ids = existing_staging.get("scenario_ids")
        if isinstance(raw_ids, dict):
            merged_ids.update({str(key): str(value) for key, value in cast(dict[object, object], raw_ids).items()})
    resolved = (
        dict(scenario_ids)
        if scenario_ids is not None
        else default_scenario_ids([task for task in profile_tasks if str(task.get("id")) in set(task_ids)])
    )
    for task_id in task_ids:
        scenario_id = resolved.get(task_id)
        if not scenario_id:
            raise KeyError(f"no scenario id for task {task_id!r}")
        if merged_ids.get(task_id, scenario_id) != scenario_id:
            raise ValueError(
                f"{staging_path}: scenario id for {task_id} changed ({merged_ids[task_id]} → {scenario_id})"
            )
        merged_ids[task_id] = scenario_id
    _write_private_json(
        staging_path,
        {"suite_tag": SUITE_TAG, "scenario_ids": {task_id: merged_ids[task_id] for task_id in sorted(merged_ids)}},
    )

    _point_tasks_symlink(matrix_dir, profile_id)
    return PreparedOutput(
        matrix_dir=matrix_dir,
        profile_dir=profile_dir,
        matrix_config_path=matrix_config_path,
        model_matrix_path=model_matrix_path,
        run_config_path=run_config_path,
        staging_scenarios_path=staging_path,
        task_ids=tuple(all_task_ids),
    )


# --------------------------------------------------------------------------------------
# Trials
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TrialRequest:
    output: Path
    task_id: str


@dataclass
class TrialOutcome:
    trial_dir: Path
    attempt: dict[str, Any]
    issues: list[str] = field(default_factory=lambda: list[str]())

    @property
    def ok(self) -> bool:
        return not self.issues and self.attempt.get("attempt_status") == "candidate_complete"


def missing_artifacts(trial_dir: Path) -> list[str]:
    return [name for name in TRIAL_ARTIFACTS if not (trial_dir / name).is_file()]


def trial_issues(trial_dir: Path) -> list[str]:
    """Missing artifacts plus snapshot artifacts the harness's own loader rejects."""

    issues = [f"missing:{name}" for name in missing_artifacts(trial_dir)]
    state_capture = harness_module("arga_twins_benchmark.evaluation.state_capture")
    for name in ("baseline-state.json", "final-state.json"):
        path = trial_dir / name
        if not path.is_file():
            continue
        try:
            state_capture.TrustedStateSnapshot.from_artifact_payload(read_json_object(path))
        except Exception as error:  # noqa: BLE001 - any loader failure is a contract issue worth listing
            issues.append(f"{name}:{type(error).__name__}:{error}")
    return issues


async def run_trials(
    requests: Sequence[TrialRequest],
    profile: Mapping[str, Any],
    candidate: Candidate,
    *,
    concurrency: int = 1,
    cli: FakeArgaCli | None = None,
    module: ModuleType | None = None,
) -> list[TrialOutcome]:
    """Prepare every output root, then run the real `run_task` for each request (gathered)."""

    if not requests:
        return []
    runner = module or load_run_script()
    arga = cli or FakeArgaCli()
    validated = validate_profile(profile)
    patched = patch_run_script(runner, cli=arga, invoke_model=candidate)
    try:
        tasks_by_id = {request.task_id.upper(): suite_task(request.task_id) for request in requests}
        scenario_ids = cast(dict[str, str], await runner.resolve_scenarios(list(tasks_by_id.values())))
        plans: list[tuple[dict[str, Any], str, Path]] = []
        for request in requests:
            task = tasks_by_id[request.task_id.upper()]
            task_id = str(task["id"])
            prepared = prepare_output_root(
                request.output,
                validated,
                [task_id],
                scenario_ids=scenario_ids,
                concurrency=concurrency,
                module=runner,
            )
            trial_dir = prepared.profile_dir / "tasks" / task_id
            if trial_dir.exists():
                raise FileExistsError(f"trial directory already exists: {trial_dir} (pick a new --output)")
            plans.append((task, scenario_ids[task_id], prepared.profile_dir))
        semaphore = asyncio.Semaphore(concurrency)
        lifecycle_semaphore = asyncio.Semaphore(LIFECYCLE_CONCURRENCY)
        cleanup_semaphore = asyncio.Semaphore(CLEANUP_CONCURRENCY)
        docs_cache = runner.OfficialDocsSnapshotCache()
        attempts = await asyncio.gather(
            *(
                runner.run_task(
                    task,
                    scenario_id=scenario_id,
                    output_root=profile_dir,
                    semaphore=semaphore,
                    lifecycle_semaphore=lifecycle_semaphore,
                    cleanup_semaphore=cleanup_semaphore,
                    docs_cache=docs_cache,
                    profile=dict(validated),
                    attempt_number=1,
                )
                for task, scenario_id, profile_dir in plans
            )
        )
    finally:
        patched.restore()
    outcomes: list[TrialOutcome] = []
    for (task, _scenario_id, profile_dir), attempt in zip(plans, attempts, strict=True):
        trial_dir = profile_dir / "tasks" / str(task["id"])
        outcomes.append(
            TrialOutcome(trial_dir=trial_dir, attempt=cast(dict[str, Any], attempt), issues=trial_issues(trial_dir))
        )
    return outcomes


async def run_trial(
    output: Path,
    task_id: str,
    profile: Mapping[str, Any],
    candidate: Candidate,
    *,
    concurrency: int = 1,
    cli: FakeArgaCli | None = None,
    module: ModuleType | None = None,
) -> Path:
    """Run one task for one profile through the real `run_task`; return the trial directory."""

    outcomes = await run_trials(
        [TrialRequest(output=Path(output), task_id=task_id)],
        profile,
        candidate,
        concurrency=concurrency,
        cli=cli,
        module=module,
    )
    return outcomes[0].trial_dir


__all__ = [
    "CLEANUP_CONCURRENCY",
    "DEVSIM_ENVIRONMENT",
    "LIFECYCLE_CONCURRENCY",
    "MATRIX_CONFIG_PROTOCOL",
    "MODULE_NAME",
    "RUN_CONFIG_PROTOCOL",
    "SWAPPED_GLOBALS",
    "TRIAL_ARTIFACTS",
    "PatchedGlobals",
    "PreparedOutput",
    "TrialOutcome",
    "TrialRequest",
    "default_scenario_ids",
    "load_run_script",
    "missing_artifacts",
    "patch_run_script",
    "prepare_output_root",
    "run_trial",
    "run_trials",
    "trial_issues",
]
