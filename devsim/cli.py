"""`python -m devsim` — serve twins, run a trial through the unmodified harness, grade, report.

python -m devsim serve  --task ECOM-02 [--seed-key K] [--print-env]
python -m devsim run    --task ECOM-02 --profile baseline-deepseek-chat --candidate stub --output runs/devsim/x
python -m devsim grade  runs/devsim/x/tasks/ECOM-02 --task ECOM-02 [--output out.json] [--via-script]
python -m devsim report runs/devsim/x reports/devsim/x --profile baseline-deepseek-chat
python -m devsim profiles
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from devsim.candidates import Provider, load_candidate
from devsim.harness import (
    GRADE_SCRIPT_RELATIVE,
    content_hash,
    harness_module,
    harness_root,
    historical_calibration_path,
    read_json_object,
    suite_path,
    suite_task,
    tasks_md_path,
)
from devsim.lifecycle import FakeArgaCli
from devsim.matrix import list_profiles, load_profile, matrix_profiles, matrix_with_profiles, write_matrix
from devsim.runner import TrialRequest, run_trials, trial_issues
from devsim.server import serve_twins

DEFAULT_TASK = "ECOM-02"


def _print(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str), flush=True)


def _eprint(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------------------
# serve
# --------------------------------------------------------------------------------------


async def _serve(task_id: str, seed_key: str | None, print_env: bool, empty: bool = False) -> int:
    task = suite_task(task_id)
    cli = FakeArgaCli()
    digest = content_hash(task)
    scenario = cli.scenario_for_digest(digest)
    raw_seed = scenario.get("seed_config")
    seed_config = cast(dict[str, Any], raw_seed) if isinstance(raw_seed, dict) else {}
    if empty:
        seed_config = {}  # evals.run / evals.contract seed the twins themselves through the seeders
    running = await serve_twins(
        [str(twin) for twin in cast(list[object], task["twins"])], seed_config, seed_key or digest
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        for line in running.env_lines(export=print_env):
            print(line, flush=True)
        _eprint(
            f"devsim: serving {', '.join(running.providers)} for {task['id']} (seed_key {running.seed_key[:12]}…); "
            "Ctrl-C to stop"
        )
        for provider, twin in sorted(running.twins.items()):
            kind = "stub" if twin.is_stub else "twin"
            _eprint(f"  {provider:<16} {kind:<4} data {twin.base_url}  admin {twin.admin_url}{twin.admin_state_path}")
        await stop.wait()
    finally:
        await running.stop()
        _eprint("devsim: stopped")
    return 0


# --------------------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------------------


async def _run(
    task_ids: Sequence[str],
    profile_id: str,
    candidate_spec: str,
    output: Path,
    repeat: int,
    concurrency: int,
) -> int:
    profile = load_profile(profile_id)
    candidate = load_candidate(candidate_spec, provider=cast(Provider, profile["provider"]))
    requests: list[TrialRequest] = []
    for index in range(1, repeat + 1):
        root = output if repeat == 1 else output / f"repeat-{index:02d}"
        requests.extend(TrialRequest(output=root, task_id=task_id) for task_id in task_ids)
    cli = FakeArgaCli()
    try:
        outcomes = await run_trials(requests, profile, candidate, concurrency=concurrency, cli=cli)
    finally:
        await cli.stop_all()
    summary = [
        {
            "trial_dir": str(outcome.trial_dir),
            "attempt_status": outcome.attempt.get("attempt_status"),
            "model_status": outcome.attempt.get("model_status"),
            "run_id": outcome.attempt.get("run_id"),
            "tool_calls": outcome.attempt.get("tool_calls"),
            "raw_state_delta_count": outcome.attempt.get("raw_state_delta_count"),
            "cleanup_succeeded": outcome.attempt.get("cleanup_succeeded"),
            "error_type": outcome.attempt.get("error_type"),
            "error": outcome.attempt.get("error"),
            "issues": outcome.issues,
        }
        for outcome in outcomes
    ]
    _print({"profile": profile_id, "candidate": candidate_spec, "trials": summary})
    return 0 if all(outcome.ok for outcome in outcomes) else 1


# --------------------------------------------------------------------------------------
# grade
# --------------------------------------------------------------------------------------


def grade_trial(trial_dir: Path, task_id: str) -> dict[str, Any]:
    """`scripts/grade_argabench_attempt.py` in-process, except that evidence_gap is reported, not raised."""

    semantic_report = harness_module("arga_twins_benchmark.reporting.argabench_semantic_report")
    task = suite_task(task_id)
    graders = semantic_report.build_domain_grader_registry(suite_path=suite_path(), tasks_path=tasks_md_path())
    prefix = str(task["id"]).split("-", 1)[0]
    domain_grade, assertions, outcome = semantic_report._grade_completed_attempt(  # noqa: SLF001
        grader=graders.get(prefix),
        task_dir=trial_dir,
        task=task,
    )
    assertions = semantic_report._enrich_structured_fact_assertions(assertions, task=task)  # noqa: SLF001
    assertions = semantic_report._enrich_unsafe_assertions(assertions, task_dir=trial_dir, task=task)  # noqa: SLF001
    assertions = semantic_report._enrich_decisive_assertion_evidence(assertions, task_dir=trial_dir)  # noqa: SLF001
    attempt_path = trial_dir / "attempt.json"
    attempt = read_json_object(attempt_path) if attempt_path.is_file() else {}
    return {
        "taskId": task["id"],
        "runId": attempt.get("run_id"),
        "profileId": attempt.get("profile_id"),
        "semanticOutcome": outcome,
        "exactReason": semantic_report._reason(outcome, assertions, None, task=task),  # noqa: SLF001
        "assertions": assertions,
        "domainGrade": domain_grade,
        "trialIssues": trial_issues(trial_dir),
    }


def _grade_via_script(trial_dir: Path, task_id: str, output: Path) -> int:
    root = harness_root()
    command = [
        "uv",
        "run",
        "--project",
        str(root),
        "python",
        str(root / GRADE_SCRIPT_RELATIVE),
        str(trial_dir.resolve()),
        "--task-id",
        task_id,
        "--output",
        str(output.resolve()),
    ]
    _eprint("devsim: " + " ".join(command))
    # The harness has its own uv project; drop this repo's VIRTUAL_ENV so uv does not warn about it.
    env = {key: value for key, value in os.environ.items() if key != "VIRTUAL_ENV"}
    completed = subprocess.run(command, cwd=root, check=False, env=env)
    return completed.returncode


def _grade(trial_dir: Path, task_id: str, output: Path | None, via_script: bool) -> int:
    if via_script:
        return _grade_via_script(trial_dir, task_id, output or trial_dir.parent / f"{trial_dir.name}-grade.json")
    result = grade_trial(trial_dir, task_id)
    if output is not None:
        harness_module("arga_twins_benchmark.lifecycle").write_private_json(output, result)
    grade = cast(dict[str, Any], result["domainGrade"])
    _print(
        {
            "taskId": result["taskId"],
            "runId": result["runId"],
            "profileId": result["profileId"],
            "semanticOutcome": result["semanticOutcome"],
            "exactReason": result["exactReason"],
            "domainGrade": {
                "outcome": grade.get("outcome"),
                "reasons": grade.get("reasons"),
                "grader_selection": grade.get("grader_selection"),
            },
            "assertions": [
                {"id": item.get("id"), "status": item.get("status"), "detail": item.get("detail")}
                for item in cast(list[dict[str, Any]], result["assertions"])
            ],
            "trialIssues": result["trialIssues"],
            "output": str(output) if output is not None else None,
        }
    )
    return 0


# --------------------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------------------


def _ensure_model_matrix(matrix_dir: Path, override: Path | None) -> Path:
    if override is not None:
        return override.resolve()
    path = matrix_dir / "model-matrix.json"
    if path.is_file():
        return path
    config_path = matrix_dir / "matrix-config.json"
    if not config_path.is_file():
        raise FileNotFoundError(f"{matrix_dir} has neither model-matrix.json nor matrix-config.json")
    configured = matrix_profiles(config_path)
    canonical_ids = {
        str(profile["id"]) for profile in matrix_profiles(harness_root() / "benchmark/argabench_40/model_matrix.json")
    }
    devsim = [profile for profile in configured if str(profile.get("id")) not in canonical_ids]
    return write_matrix(path, matrix_with_profiles(devsim))


def build_report(matrix_dir: Path, report_dir: Path, *, model_matrix: Path, published_at: str | None) -> dict[str, Any]:
    semantic_report = harness_module("arga_twins_benchmark.reporting.argabench_semantic_report")
    report = cast(
        dict[str, Any],
        semantic_report.build_argabench_semantic_report(
            matrix_dir,
            suite_path=suite_path(),
            tasks_path=tasks_md_path(),
            model_matrix_path=model_matrix,
            historical_calibration_path=historical_calibration_path(),
        ),
    )
    outputs = cast(
        dict[str, Any],
        semantic_report.write_argabench_semantic_report(
            report,
            report_dir,
            source_matrix_dir=matrix_dir,
            suite_path=suite_path(),
            published_at=published_at,
        ),
    )
    report["_outputs"] = outputs
    return report


def profile_summary(report: dict[str, Any], profile_id: str) -> dict[str, Any]:
    attempts = [
        item for item in cast(list[dict[str, Any]], report.get("attempts", [])) if item.get("profile_id") == profile_id
    ]
    profile_report = cast(dict[str, dict[str, Any]], report.get("profiles", {})).get(profile_id, {})
    return {
        "profile_id": profile_id,
        "scoring_ready": profile_report.get("scoring_ready"),
        "runtime": profile_report.get("runtime"),
        "validity": profile_report.get("validity"),
        "semantic": profile_report.get("semantic"),
        "matrix_integrity_issues": cast(dict[str, Any], report.get("execution_classification", {})).get(
            "matrix_integrity_issues"
        ),
        "trials": [
            {
                "task_id": item.get("task_id"),
                "execution_class": item.get("execution_class"),
                "validity": item.get("validity"),
                "score_eligible": item.get("score_eligible"),
                "semantic_outcome": item.get("semantic_outcome"),
                "model_status": item.get("model_status"),
                "integrity_issues": cast(dict[str, Any], item.get("integrity", {})).get("issues"),
                "evidence_gaps": item.get("evidence_gaps"),
                "reason": item.get("reason"),
                "assertions": [
                    {"id": entry.get("id"), "status": entry.get("status"), "detail": entry.get("detail")}
                    for entry in cast(list[dict[str, Any]], item.get("assertions", []))
                ],
            }
            for item in attempts
        ],
    }


def _report(
    matrix_dir: Path, report_dir: Path, profile_id: str, model_matrix: Path | None, published_at: str | None
) -> int:
    matrix_dir = matrix_dir.resolve()
    matrix_path = _ensure_model_matrix(matrix_dir, model_matrix)
    report = build_report(matrix_dir, report_dir.resolve(), model_matrix=matrix_path, published_at=published_at)
    summary = profile_summary(report, profile_id)
    summary["outputs"] = report["_outputs"]
    summary["model_matrix"] = str(matrix_path)
    _print(summary)
    return 0


# --------------------------------------------------------------------------------------
# argparse
# --------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m devsim", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="start the task's twins on loopback and print their URLs")
    serve.add_argument("--task", default=DEFAULT_TASK)
    serve.add_argument(
        "--seed-key", default=None, help="override the deterministic seed key (default: task content hash)"
    )
    serve.add_argument("--empty", action="store_true", help="start the twins unseeded (evals.run seeds them itself)")
    serve.add_argument("--print-env", action="store_true", help="print `export DEVSIM_<PROVIDER>_URL=...` lines")

    run = commands.add_parser("run", help="run one or more tasks through the unmodified run_task")
    run.add_argument("--task", dest="tasks", action="append", help="task id (repeatable); default ECOM-02")
    run.add_argument("--profile", required=True, help="canonical or devsim profile id (see `profiles`)")
    run.add_argument("--candidate", default="stub", help="stub | scripted:<json file> | module:<dotted.path>:<factory>")
    run.add_argument("--output", type=Path, required=True, help="matrix directory (profiles/<id>/tasks/<task> inside)")
    run.add_argument("--repeat", type=int, default=1, help="repeat count; >1 writes <output>/repeat-NN matrix dirs")
    run.add_argument("--concurrency", type=int, default=1, help="parallel trials (1-16)")

    grade = commands.add_parser("grade", help="grade one trial directory with the harness's semantic grader")
    grade.add_argument("trial_dir", type=Path)
    grade.add_argument("--task", required=True)
    grade.add_argument("--output", type=Path, default=None)
    grade.add_argument(
        "--via-script", action="store_true", help="run scripts/grade_argabench_attempt.py as a subprocess"
    )

    report = commands.add_parser("report", help="build the offline semantic report for a matrix directory")
    report.add_argument("matrix_dir", type=Path)
    report.add_argument("report_dir", type=Path)
    report.add_argument("--profile", required=True)
    report.add_argument(
        "--model-matrix",
        type=Path,
        default=None,
        help="37-profile matrix copy (default: <matrix_dir>/model-matrix.json)",
    )
    report.add_argument("--published-at", default=None)

    commands.add_parser("profiles", help="list selectable profiles")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        return asyncio.run(_serve(args.task, args.seed_key, args.print_env, bool(args.empty)))
    if args.command == "run":
        if args.repeat < 1:
            raise SystemExit("--repeat must be >= 1")
        return asyncio.run(
            _run(args.tasks or [DEFAULT_TASK], args.profile, args.candidate, args.output, args.repeat, args.concurrency)
        )
    if args.command == "grade":
        return _grade(args.trial_dir, args.task, args.output, args.via_script)
    if args.command == "report":
        return _report(args.matrix_dir, args.report_dir, args.profile, args.model_matrix, args.published_at)
    if args.command == "profiles":
        _print(
            {
                profile_id: {
                    key: profile.get(key) for key in ("provider", "model_id", "api_effort", "thinking", "agent")
                }
                for profile_id, profile in list_profiles().items()
            }
        )
        return 0
    raise SystemExit(f"unknown command {args.command!r}")


__all__ = ["build_parser", "build_report", "grade_trial", "main", "profile_summary"]
