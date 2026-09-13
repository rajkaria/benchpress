"""Turn a tree of trial directories into the comparison a judge can check.

    python -m evals.compare runs/ --out reports/

Writes three files:

- `results.json`      every trial as one row, plus the aggregate, so the tables can be recomputed.
- `compare.md`        scenario × arm: pass / fail / unsafe / unscored, the per-assertion failure
                      frequency, median provider calls and latency, mean cost, and a
                      published-context column.
- `leaderboard-row.md` the single headline line for the submission.

**Claim discipline.** The comparator is the same-substrate baseline in this table.
The published column is ArgaBench's own published result for the underlying task and is context
only — it is a different substrate and a different grader, and this file never presents the two as
the same measurement. Verdicts of `unscored` are counted and shown, never silently dropped.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORTS = REPO_ROOT / "reports"
OUTCOMES: tuple[str, ...] = ("pass", "fail", "unsafe", "unscored")

# ArgaBench's published results for the underlying tasks, out of 111 published trials across all 37
# configurations (verified 2026-09-13).
# Context only: different substrate, different grader. Never a comparator.
PUBLISHED_CONTEXT: dict[str, str] = {
    "CRM-02": "0/111 published",
    "CRM-05": "0/111 published",
    "ECOM-02": "0/111 published",
    "DEV-03": "90/111 unsafe published",
}
PUBLISHED_UNKNOWN = "not published"


@dataclass
class TrialRow:
    scenario: str
    task_id: str
    agent: str
    arm: str
    ablations: tuple[str, ...]
    repeat: int
    substrate: str
    model: str
    outcome: str
    status: str
    provider_calls: int
    docs_calls: int
    latency_ms: int
    cost_usd: float
    failed_assertions: tuple[str, ...]
    trial_dir: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "task_id": self.task_id,
            "agent": self.agent,
            "arm": self.arm,
            "ablations": list(self.ablations),
            "repeat": self.repeat,
            "substrate": self.substrate,
            "model": self.model,
            "outcome": self.outcome,
            "status": self.status,
            "provider_calls": self.provider_calls,
            "docs_calls": self.docs_calls,
            "latency_ms": self.latency_ms,
            "cost_usd": self.cost_usd,
            "failed_assertions": list(self.failed_assertions),
            "trial_dir": self.trial_dir,
        }


@dataclass
class Group:
    scenario: str
    task_id: str
    arm: str
    substrate: str = "real"
    rows: list[TrialRow] = field(default_factory=lambda: list[TrialRow]())

    @property
    def counts(self) -> dict[str, int]:
        tally = {outcome: 0 for outcome in OUTCOMES}
        for row in self.rows:
            tally[row.outcome if row.outcome in tally else "unscored"] += 1
        return tally

    @property
    def assertion_failures(self) -> dict[str, int]:
        failures: dict[str, int] = {}
        for row in self.rows:
            for assertion_id in row.failed_assertions:
                failures[assertion_id] = failures.get(assertion_id, 0) + 1
        return dict(sorted(failures.items(), key=lambda item: (-item[1], item[0])))

    def median(self, attribute: str) -> float:
        values = [float(getattr(row, attribute)) for row in self.rows]
        return round(statistics.median(values), 2) if values else 0.0

    def mean_cost(self) -> float:
        values = [row.cost_usd for row in self.rows]
        return round(statistics.fmean(values), 4) if values else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "task_id": self.task_id,
            "arm": self.arm,
            "trials": len(self.rows),
            "counts": self.counts,
            "assertion_failures": self.assertion_failures,
            "median_provider_calls": self.median("provider_calls"),
            "median_latency_s": round(self.median("latency_ms") / 1000, 1),
            "mean_cost_usd": self.mean_cost(),
            "published_context": PUBLISHED_CONTEXT.get(self.task_id, PUBLISHED_UNKNOWN),
        }


# --------------------------------------------------------------------------------------
# Collection
# --------------------------------------------------------------------------------------


def find_trials(root: Path) -> list[Path]:
    """Every directory under `root` that holds a `verdict.json`, in stable path order."""
    return sorted({path.parent for path in root.rglob("verdict.json")})


def load_row(trial_dir: Path) -> TrialRow | None:
    verdict = _read(trial_dir / "verdict.json")
    if not verdict:
        return None
    trial = _read(trial_dir / "trial.json")
    invocation = _read(trial_dir / "invocation.json")
    trace = _read(trial_dir / "trace.json")

    usage = _mapping(invocation.get("usage"))
    config = _mapping(invocation.get("config"))
    ablations = tuple(str(item) for item in _list(trial.get("ablations")))
    agent = str(trial.get("agent") or _infer_agent(trial_dir))
    events = _list(trace.get("events"))
    docs_calls = sum(1 for event in events if _mapping(event).get("kind") == "provider_docs")

    return TrialRow(
        scenario=str(trial.get("scenario") or trial_dir.parent.parent.name),
        task_id=str(trial.get("task_id") or ""),
        agent=agent,
        arm=str(trial.get("arm") or trial_dir.parent.name),
        ablations=ablations,
        repeat=_int(trial.get("repeat"), default=1),
        substrate=str(trial.get("substrate") or "real"),
        model=str(config.get("model") or trial.get("model") or ""),
        outcome=str(verdict.get("outcome") or "unscored"),
        status=str(invocation.get("status") or "unknown"),
        provider_calls=len(events) - docs_calls,
        docs_calls=docs_calls,
        latency_ms=_int(invocation.get("latency_ms")),
        cost_usd=_float(usage.get("cost_usd")),
        failed_assertions=_failed_assertions(verdict),
        trial_dir=str(trial_dir),
    )


def collect(root: Path) -> list[TrialRow]:
    rows: list[TrialRow] = []
    for trial_dir in find_trials(root):
        row = load_row(trial_dir)
        if row is not None:
            rows.append(row)
    return rows


def group(rows: Iterable[TrialRow]) -> list[Group]:
    groups: dict[tuple[str, str, str], Group] = {}
    for row in rows:
        key = (row.substrate, row.scenario, row.arm)
        bucket = groups.get(key)
        if bucket is None:
            bucket = Group(scenario=row.scenario, task_id=row.task_id, arm=row.arm, substrate=row.substrate)
            groups[key] = bucket
        if not bucket.task_id:
            bucket.task_id = row.task_id
        bucket.rows.append(row)
    return [groups[key] for key in sorted(groups)]


# --------------------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------------------


def render_compare(groups: Sequence[Group]) -> str:
    lines: list[str] = [
        "# Benchpress vs stock baseline — same substrate, same prompts, same limits",
        "",
        "Scored by `evals/assertions.py`, our port of ArgaBench's pass/unsafe criteria, on the",
        "published seed rebuilt locally. Not the official grader; no leaderboard claim is made.",
        "The published column is ArgaBench's own published number for the underlying task and is",
        "**context only** — different substrate, different grader.",
        "",
        (
            "| substrate | scenario | arm | trials | pass | fail | unsafe | unscored | median calls | "
            "median latency | mean cost | published (context) |"
        ),
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for bucket in groups:
        counts = bucket.counts
        lines.append(
            f"| {bucket.substrate} | {bucket.scenario} | `{bucket.arm}` | {len(bucket.rows)} | "
            f"{counts['pass']} | {counts['fail']} | {counts['unsafe']} | {counts['unscored']} | "
            f"{bucket.median('provider_calls'):.0f} | {bucket.median('latency_ms') / 1000:.0f} s | "
            f"${bucket.mean_cost():.2f} | {PUBLISHED_CONTEXT.get(bucket.task_id, PUBLISHED_UNKNOWN)} |"
        )

    lines += ["", "## Per-assertion failure frequency", ""]
    any_failures = False
    for bucket in groups:
        failures = bucket.assertion_failures
        if not failures:
            continue
        any_failures = True
        detail = ", ".join(f"`{name}` ×{count}" for name, count in failures.items())
        lines.append(f"- **{bucket.scenario} / `{bucket.arm}`** ({len(bucket.rows)} trials): {detail}")
    if not any_failures:
        lines.append("- no assertion failed in any scored trial")

    unscored = sum(bucket.counts["unscored"] for bucket in groups)
    if unscored:
        lines += ["", f"> {unscored} trial(s) are **unscored**: they ran, but no verdict was produced.", ""]
    return "\n".join(lines) + "\n"


def render_leaderboard_row(groups: Sequence[Group]) -> str:
    """The one line that goes in the submission, with its caveat attached."""
    lines = [
        "# Leaderboard row (Plan B substrate)",
        "",
        "| scenario | Benchpress | stock baseline | substrate | grader |",
        "|---|---|---|---|---|",
    ]
    scenarios = sorted({bucket.scenario for bucket in groups})
    for scenario in scenarios:
        benchpress = next((b for b in groups if b.scenario == scenario and b.arm == "benchpress"), None)
        baseline = next((b for b in groups if b.scenario == scenario and b.arm == "baseline"), None)
        substrate = next(
            (row.substrate for bucket in groups if bucket.scenario == scenario for row in bucket.rows),
            "real",
        )
        lines.append(
            f"| {scenario} | {_score_cell(benchpress)} | {_score_cell(baseline)} | "
            f"{substrate} apps, published seed rebuilt locally | ported ArgaBench assertions |"
        )
    lines += [
        "",
        "Graded by our port of ArgaBench's verifier on the published seed rebuilt locally, not by the",
        "official grader. No claim is made about the official leaderboard.",
    ]
    return "\n".join(lines) + "\n"


def _score_cell(bucket: Group | None) -> str:
    if bucket is None or not bucket.rows:
        return "—"
    counts = bucket.counts
    cell = f"{counts['pass']}/{len(bucket.rows)} pass"
    if counts["unsafe"]:
        cell += f", {counts['unsafe']} unsafe"
    if counts["unscored"]:
        cell += f", {counts['unscored']} unscored"
    return cell


def results_payload(rows: Sequence[TrialRow], groups: Sequence[Group]) -> dict[str, Any]:
    return {
        "protocol": "benchpress-compare/1",
        "trials": len(rows),
        "claim": (
            "same-substrate comparison; scored by our port of ArgaBench's pass/unsafe criteria on the "
            "published seed rebuilt locally, not by the official grader"
        ),
        "groups": [bucket.as_dict() for bucket in groups],
        "rows": [row.as_dict() for row in rows],
    }


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _failed_assertions(verdict: Mapping[str, Any]) -> tuple[str, ...]:
    failed: list[str] = []
    for item in _list(verdict.get("assertions")):
        assertion = _mapping(item)
        if assertion.get("ok") is False:
            failed.append(str(assertion.get("id") or assertion.get("check") or "?"))
    return tuple(failed)


def _infer_agent(trial_dir: Path) -> str:
    return trial_dir.parent.name.split("+", 1)[0]


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data: object = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}
    return cast(dict[str, Any], data) if isinstance(data, dict) else {}


def _mapping(value: object) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _list(value: object) -> list[Any]:
    return cast(list[Any], value) if isinstance(value, list) else []


def _int(value: object, *, default: int = 0) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _float(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m evals.compare", description="Compare trial verdicts.")
    parser.add_argument("runs", nargs="?", default="runs", help="run root to scan (default: runs/)")
    parser.add_argument("--out", default=str(DEFAULT_REPORTS), help="report directory (default: reports/)")
    return parser.parse_args(argv)


def write_reports(rows: Sequence[TrialRow], out_dir: Path) -> list[Path]:
    buckets = group(rows)
    out_dir.mkdir(parents=True, exist_ok=True)
    results = out_dir / "results.json"
    compare = out_dir / "compare.md"
    leaderboard = out_dir / "leaderboard-row.md"
    results.write_text(json.dumps(results_payload(rows, buckets), indent=2, sort_keys=True) + "\n")
    compare.write_text(render_compare(buckets))
    leaderboard.write_text(render_leaderboard_row(buckets))
    return [results, compare, leaderboard]


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    root = Path(str(args.runs))
    if not root.exists():
        print(f"error: {root} does not exist", file=sys.stderr)
        return 2
    rows = collect(root)
    if not rows:
        print(f"error: no verdict.json found under {root}", file=sys.stderr)
        return 1
    for path in write_reports(rows, Path(str(args.out))):
        print(f"wrote:{path}")
    print(f"trials={len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
