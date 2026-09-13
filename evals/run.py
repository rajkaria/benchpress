"""The run loop: schedule trials, run them strictly sequentially, append progress.

    python -m evals.run --scenario billing-review --agent benchpress --repeats 1
    python -m evals.run --scenario billing-review-injection --agent baseline --apps slack,stripe,hubspot
    python -m evals.run --scenario billing-review --agent benchpress --ablations no_gate --no-reset
    python -m evals.run --matrix plan-b                      # the rehearsal matrix, in cut order

Trials are sequential by construction: one set of scratch accounts is the whole substrate, so two
concurrent trials would seed on top of each other. `--matrix plan-b` runs the rehearsal matrix in order and
appends one line to `reports/progress.jsonl` after each trial, so whatever has finished when the
clock runs out is already reported and the count is honest.

`--substrate devsim` points the gateway at local twins through `DEVSIM_<PROVIDER>_URL`
(`benchpress.realapp.gateway_from_env`); the run refuses to start if any of those are unset, rather
than silently addressing the real SaaS hosts.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from dotenv import load_dotenv

from evals.trial import (
    DEFAULT_APPS,
    DEFAULT_RUNS,
    KNOWN_AGENTS,
    TrialHooks,
    TrialOutcome,
    TrialSpec,
    run_trial,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PROGRESS = REPO_ROOT / "reports" / "progress.jsonl"

# The Plan B rehearsal matrix, in order: the schedule is also the cut list.
PLAN_B_MATRIX: tuple[tuple[str, str, int, tuple[str, ...]], ...] = (
    ("billing-review", "benchpress", 1, ()),
    ("billing-review", "baseline", 1, ()),
    ("billing-review-injection", "benchpress", 1, ()),
    ("billing-review-injection", "baseline", 1, ()),
    ("billing-review", "benchpress", 2, ()),
    ("billing-review", "baseline", 2, ()),
    ("billing-review", "benchpress", 1, ("no_policy_sweep",)),
    ("billing-review-injection", "benchpress", 1, ("no_gate",)),
    ("billing-review", "benchpress", 3, ()),
    ("billing-review", "baseline", 3, ()),
    ("billing-review", "benchpress", 1, ("no_readback",)),
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m evals.run", description="Run Plan B rehearsal trials.")
    parser.add_argument("--scenario", help="evals.scenarios id, e.g. billing-review")
    parser.add_argument("--agent", choices=KNOWN_AGENTS, help="which arm to run")
    parser.add_argument("--repeats", type=int, default=1, help="repeat count (sequential)")
    parser.add_argument("--ablations", default="", help="comma-separated: no_policy_sweep,no_gate,no_readback")
    parser.add_argument("--apps", default=",".join(DEFAULT_APPS), help="comma-separated apps to seed/snapshot/reset")
    parser.add_argument("--substrate", choices=("real", "devsim"), default="real")
    parser.add_argument("--model", default=None, help="model id override (default: $BENCHPRESS_MODEL)")
    parser.add_argument("--out", default=str(DEFAULT_RUNS), help="run root (default: runs/)")
    parser.add_argument("--no-score", action="store_true", help="skip evals.assertions scoring")
    parser.add_argument("--no-reset", action="store_true", help="leave the apps seeded after the trial")
    parser.add_argument("--matrix", choices=("plan-b",), help="run a named matrix instead of one scenario")
    parser.add_argument("--dry-run", action="store_true", help="print the schedule and exit")
    return parser.parse_args(argv)


def build_specs(args: argparse.Namespace) -> list[TrialSpec]:
    apps = tuple(part.strip().lower() for part in str(args.apps).split(",") if part.strip())
    if not apps:
        raise ValueError("--apps must name at least one app")

    def spec(scenario: str, agent: str, repeat: int, ablations: tuple[str, ...]) -> TrialSpec:
        return TrialSpec(
            scenario_id=scenario,
            agent=agent,
            repeat=repeat,
            ablations=ablations,
            apps=apps,
            model=args.model,
            substrate=str(args.substrate),
            score=not args.no_score,
            reset=not args.no_reset,
            out_root=Path(str(args.out)),
        )

    if args.matrix == "plan-b":
        return [spec(*entry) for entry in PLAN_B_MATRIX]
    if not args.scenario or not args.agent:
        raise ValueError("--scenario and --agent are required unless --matrix is given")
    ablations = tuple(part.strip() for part in str(args.ablations).split(",") if part.strip())
    repeats = max(1, int(args.repeats))
    return [spec(str(args.scenario), str(args.agent), index, ablations) for index in range(1, repeats + 1)]


def check_substrate(specs: Sequence[TrialSpec], env: dict[str, str]) -> list[str]:
    """Missing `DEVSIM_<PROVIDER>_URL` variables for a devsim run (empty when fine)."""
    missing: list[str] = []
    for spec in specs:
        if spec.substrate != "devsim":
            continue
        for app in spec.apps:
            name = f"DEVSIM_{app.upper()}_URL"
            if not env.get(name) and name not in missing:
                missing.append(name)
    return missing


def append_progress(outcome: TrialOutcome, path: Path = PROGRESS) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(outcome.summary(), sort_keys=True) + "\n")


async def run_all(
    specs: Sequence[TrialSpec],
    *,
    hooks: TrialHooks | None = None,
    progress_path: Path | None = None,
) -> list[TrialOutcome]:
    outcomes: list[TrialOutcome] = []
    for index, spec in enumerate(specs, start=1):
        print(f"[{index}/{len(specs)}] {spec.scenario_id} × {spec.arm} r{spec.repeat} …", flush=True)
        outcome = await run_trial(spec, hooks=hooks)
        outcomes.append(outcome)
        if progress_path is not None:
            append_progress(outcome, progress_path)
        print(
            f"    outcome={outcome.outcome} status={outcome.status} "
            f"calls={outcome.provider_calls} {outcome.elapsed_s:.0f}s -> {outcome.trial_dir}",
            flush=True,
        )
        for note in outcome.notes:
            print(f"    {note}", flush=True)
    return outcomes


async def main_async(argv: Sequence[str] | None = None, *, hooks: TrialHooks | None = None) -> int:
    args = parse_args(argv)
    load_dotenv(REPO_ROOT / ".env")
    specs = build_specs(args)

    missing = check_substrate(specs, dict(os.environ))
    if missing:
        print(f"error: --substrate devsim needs {', '.join(missing)}", file=sys.stderr)
        return 2

    if args.dry_run:
        for spec in specs:
            print(f"{spec.scenario_id} × {spec.arm} r{spec.repeat} apps={','.join(spec.apps)}")
        return 0

    outcomes = await run_all(
        specs,
        hooks=hooks,
        progress_path=PROGRESS if args.matrix else None,
    )
    failed = [item for item in outcomes if item.error]
    print(f"trials={len(outcomes)} errors={len(failed)}")
    return 1 if failed else 0


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return asyncio.run(main_async(argv))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
