"""Roll every scored trial into `reports/summary.json` and `reports/summary.md`.

    uv run python scripts/summarize_reports.py [--runs runs] [--reports reports]

Sources: real-app trials written by `evals.run` (`runs/<substrate>/<scenario>/<arm>/<trial>/verdict.json`)
and ArgaBench semantic reports over the local twins (`reports/devsim/<arm>/repeat-NN/semantic-report.json`).
Contaminated or superseded trials are excluded by moving them out of `runs/`; nothing here filters.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast


def _read(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def real_trials(runs: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for verdict_path in sorted(runs.glob("*/*/*/*/verdict.json")):
        trial_dir = verdict_path.parent
        substrate, scenario, arm = trial_dir.parts[-4], trial_dir.parts[-3], trial_dir.parts[-2]
        verdict = _read(verdict_path)
        trial = _read(trial_dir / "trial.json") if (trial_dir / "trial.json").exists() else {}
        invocation = _read(trial_dir / "invocation.json") if (trial_dir / "invocation.json").exists() else {}
        receipt = _read(trial_dir / "receipt.json") if (trial_dir / "receipt.json").exists() else {}
        failing = [a["id"] for a in cast(list[dict[str, Any]], verdict.get("assertions", [])) if not a.get("ok", True)]
        rows.append(
            {
                "substrate": substrate,
                "scenario": scenario,
                "arm": arm,
                "trial": trial_dir.name,
                "outcome": verdict.get("outcome"),
                "failing_assertions": failing,
                "agent_status": receipt.get("status") or invocation.get("status"),
                "escalation_reason": receipt.get("escalation_reason"),
                "tool_calls": invocation.get("tool_calls"),
                "latency_ms": invocation.get("latency_ms"),
                "model": trial.get("model") or invocation.get("requested_model"),
                "path": str(trial_dir),
            }
        )
    return rows


def twin_trials(reports: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for report_path in sorted(reports.glob("devsim/*/repeat-*/semantic-report.json")):
        arm, repeat = report_path.parts[-3], report_path.parts[-2]
        report = _read(report_path)
        for attempt in cast(list[dict[str, Any]], report.get("attempts", [])):
            profile = str(attempt.get("profile_id", ""))
            if not profile.startswith(arm):
                continue
            assertions = cast(list[dict[str, Any]], attempt.get("assertions", []))
            failing = [
                f"{a.get('id')}:{a.get('status')}"
                for a in assertions
                if a.get("status") not in ("pass", "satisfied", "ok")
            ]
            totals = cast(dict[str, Any], report.get("totals", {}))
            semantic = cast(dict[str, Any], totals.get("semantic", {}))
            usage = cast(dict[str, Any], totals.get("usage", {}))
            outcome = next((k for k in ("unsafe", "fail", "pass") if int(semantic.get(k, 0) or 0) > 0), "unscored")
            rows.append(
                {
                    "substrate": "devsim-harness",
                    "grader": "ArgaBench semantic report (unmodified)",
                    "arm": arm,
                    "repeat": repeat,
                    "profile": profile,
                    "outcome": outcome,
                    "failing_assertions": failing,
                    "provider_tool_calls": usage.get("provider_tool_calls"),
                    "docs_tool_calls": usage.get("official_docs_tool_calls"),
                    "estimated_cost_usd": usage.get("estimated_cost_usd"),
                    "path": str(report_path),
                }
            )
    return rows


def tally(rows: Sequence[dict[str, Any]], key: str) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for row in rows:
        bucket = out.setdefault(f"{row['substrate']}/{row[key]}", {"pass": 0, "fail": 0, "unsafe": 0, "unscored": 0})
        bucket[str(row.get("outcome") or "unscored")] = bucket.get(str(row.get("outcome") or "unscored"), 0) + 1
    return out


def render_md(summary: dict[str, Any]) -> str:
    lines = [
        "# Summary of scored trials",
        "",
        "| substrate / arm | pass | fail | unsafe | unscored |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, counts in summary["tally"].items():
        lines.append(f"| {name} | {counts['pass']} | {counts['fail']} | {counts['unsafe']} | {counts['unscored']} |")
    lines += [
        "",
        "## Trials",
        "",
        "| substrate | arm | trial | outcome | failing assertions | agent status |",
        "|---|---|---|---|---|---|",
    ]
    for row in summary["real_trials"]:
        lines.append(
            f"| {row['substrate']} | {row['arm']} | {row['trial']} | {row['outcome']} | "
            f"{', '.join(row['failing_assertions']) or '—'} | {row.get('agent_status') or '—'} |"
        )
    for row in summary["twin_trials"]:
        lines.append(
            f"| {row['substrate']} | {row['arm']} | {row['repeat']} | {row['outcome']} | "
            f"{', '.join(row['failing_assertions']) or '—'} | — |"
        )
    lines += ["", "Outcomes: unsafe > fail > pass. See `reports/INDEX.md` for how to verify any row."]
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", default="runs")
    parser.add_argument("--reports", default="reports")
    args = parser.parse_args(argv)
    runs, reports = Path(args.runs), Path(args.reports)
    real = real_trials(runs)
    twins = twin_trials(reports)
    summary = {
        "real_trials": real,
        "twin_trials": twins,
        "tally": tally(real, "arm") | tally(twins, "arm"),
    }
    (reports / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (reports / "summary.md").write_text(render_md(summary), encoding="utf-8")
    print(json.dumps(summary["tally"], indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
