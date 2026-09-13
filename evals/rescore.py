"""Re-score existing trial directories with the current `evals.assertions.score`.

    python -m evals.rescore runs/real/billing-review/benchpress/<trial> [...]

Rewrites `verdict.json` in place (the previous verdict is kept as `verdict.prev.json`) and prints
`<trial_dir> outcome=<pass|fail|unsafe>`. Trials are never re-run; only the scoring is refreshed,
which is what you want after fixing an assertion.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from evals import scenarios
from evals.assertions import score


def _read(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def rescore(trial_dir: Path) -> str:
    trial = _read(trial_dir / "trial.json")
    invocation = _read(trial_dir / "invocation.json")
    scenario_id = str(trial.get("scenario") or trial.get("scenario_id") or "billing-review")
    task = scenarios.load(scenario_id).task
    verdict = score(
        task,
        trace=cast(list[Any], _read(trial_dir / "trace.json").get("events", [])),
        events=cast(list[Any], invocation.get("events", [])),
        state_before=_read(trial_dir / "state-before.json"),
        state_after=_read(trial_dir / "state-after.json"),
        final_text=str(invocation.get("final_text", "")),
    )
    previous = trial_dir / "verdict.json"
    if previous.exists():
        (trial_dir / "verdict.prev.json").write_text(previous.read_text(encoding="utf-8"), encoding="utf-8")
    previous.write_text(json.dumps(verdict.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return verdict.outcome


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.rescore", description=__doc__)
    parser.add_argument("trial_dirs", nargs="+", help="trial directories written by evals.run")
    args = parser.parse_args(argv)
    for raw in args.trial_dirs:
        trial_dir = Path(raw)
        if not (trial_dir / "state-after.json").exists():
            print(f"{trial_dir} skipped (no state-after.json)", file=sys.stderr)
            continue
        print(f"{trial_dir} outcome={rescore(trial_dir)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
