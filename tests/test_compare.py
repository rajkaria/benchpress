"""The comparison tables: grouping, medians, unscored tolerance and claim discipline."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evals import compare


def write_trial(
    root: Path,
    *,
    scenario: str = "widget-review",
    arm: str = "benchpress",
    agent: str | None = None,
    repeat: int = 1,
    task_id: str = "ECOM-02",
    outcome: str = "pass",
    assertions: list[dict[str, Any]] | None = None,
    provider_calls: int = 3,
    docs_calls: int = 1,
    latency_ms: int = 120_000,
    cost_usd: float = 1.25,
    status: str = "completed",
) -> Path:
    trial_dir = root / scenario / arm / f"20260913T0000{repeat:02d}-r{repeat}"
    trial_dir.mkdir(parents=True, exist_ok=True)
    (trial_dir / "trial.json").write_text(
        json.dumps(
            {
                "scenario": scenario,
                "task_id": task_id,
                "agent": agent or arm.split("+", 1)[0],
                "arm": arm,
                "ablations": arm.split("+", 1)[1].split(",") if "+" in arm else [],
                "repeat": repeat,
                "substrate": "real",
            }
        )
    )
    (trial_dir / "verdict.json").write_text(
        json.dumps({"outcome": outcome, "assertions": assertions if assertions is not None else []})
    )
    (trial_dir / "invocation.json").write_text(
        json.dumps(
            {
                "status": status,
                "latency_ms": latency_ms,
                "usage": {"cost_usd": cost_usd},
                "config": {"model": "test-model"},
            }
        )
    )
    (trial_dir / "trace.json").write_text(
        json.dumps(
            {
                "events": [{"kind": "provider_api", "sequence": i} for i in range(provider_calls)]
                + [{"kind": "provider_docs", "sequence": i} for i in range(docs_calls)]
            }
        )
    )
    return trial_dir


def test_finds_every_trial_and_splits_calls_by_kind(tmp_path: Path) -> None:
    write_trial(tmp_path, provider_calls=5, docs_calls=2)
    rows = compare.collect(tmp_path)
    assert len(rows) == 1
    assert rows[0].provider_calls == 5
    assert rows[0].docs_calls == 2
    assert rows[0].model == "test-model"


def test_groups_by_scenario_and_arm_with_ablations_separate(tmp_path: Path) -> None:
    write_trial(tmp_path, arm="benchpress", repeat=1)
    write_trial(tmp_path, arm="benchpress", repeat=2)
    write_trial(tmp_path, arm="baseline", repeat=1, outcome="fail")
    write_trial(tmp_path, arm="benchpress+no_gate", repeat=1, outcome="unsafe")
    groups = compare.group(compare.collect(tmp_path))
    arms = {bucket.arm: bucket for bucket in groups}
    assert set(arms) == {"benchpress", "baseline", "benchpress+no_gate"}
    assert arms["benchpress"].counts["pass"] == 2
    assert arms["baseline"].counts["fail"] == 1
    assert arms["benchpress+no_gate"].counts["unsafe"] == 1


def test_unscored_verdicts_are_counted_never_dropped(tmp_path: Path) -> None:
    write_trial(tmp_path, outcome="unscored")
    write_trial(tmp_path, repeat=2, outcome="pass")
    groups = compare.group(compare.collect(tmp_path))
    assert groups[0].counts == {"pass": 1, "fail": 0, "unsafe": 0, "unscored": 1}
    text = compare.render_compare(groups)
    assert "1 trial(s) are **unscored**" in text


def test_unknown_outcome_is_bucketed_as_unscored(tmp_path: Path) -> None:
    write_trial(tmp_path, outcome="weird")
    groups = compare.group(compare.collect(tmp_path))
    assert groups[0].counts["unscored"] == 1


def test_per_assertion_failure_frequency(tmp_path: Path) -> None:
    fails = [{"id": "A5", "ok": False}, {"id": "A8", "ok": False}, {"id": "A1", "ok": True}]
    write_trial(tmp_path, arm="baseline", repeat=1, outcome="fail", assertions=fails)
    write_trial(tmp_path, arm="baseline", repeat=2, outcome="fail", assertions=[{"id": "A5", "ok": False}])
    groups = compare.group(compare.collect(tmp_path))
    assert groups[0].assertion_failures == {"A5": 2, "A8": 1}
    text = compare.render_compare(groups)
    assert "`A5` ×2" in text


def test_medians_and_mean_cost(tmp_path: Path) -> None:
    write_trial(tmp_path, repeat=1, provider_calls=10, latency_ms=100_000, cost_usd=1.0)
    write_trial(tmp_path, repeat=2, provider_calls=20, latency_ms=300_000, cost_usd=3.0)
    write_trial(tmp_path, repeat=3, provider_calls=30, latency_ms=200_000, cost_usd=2.0)
    bucket = compare.group(compare.collect(tmp_path))[0]
    assert bucket.median("provider_calls") == 20.0
    assert bucket.median("latency_ms") == 200_000.0
    assert bucket.mean_cost() == 2.0


def test_published_context_column_is_present_and_labelled(tmp_path: Path) -> None:
    write_trial(tmp_path, task_id="ECOM-02")
    text = compare.render_compare(compare.group(compare.collect(tmp_path)))
    assert "0/111 published" in text
    assert "context only" in text
    leaderboard = compare.render_leaderboard_row(compare.group(compare.collect(tmp_path)))
    assert "No claim is made about the official leaderboard." in leaderboard
    assert "ported ArgaBench assertions" in leaderboard


def test_unknown_task_gets_no_published_claim(tmp_path: Path) -> None:
    write_trial(tmp_path, task_id="NEW-99")
    text = compare.render_compare(compare.group(compare.collect(tmp_path)))
    assert "not published" in text


def test_leaderboard_row_compares_the_two_arms(tmp_path: Path) -> None:
    write_trial(tmp_path, arm="benchpress", repeat=1, outcome="pass")
    write_trial(tmp_path, arm="baseline", repeat=1, outcome="unsafe")
    text = compare.render_leaderboard_row(compare.group(compare.collect(tmp_path)))
    assert "1/1 pass" in text
    assert "0/1 pass, 1 unsafe" in text


def test_cli_writes_all_three_reports(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    write_trial(runs)
    out = tmp_path / "reports"
    assert compare.main([str(runs), "--out", str(out)]) == 0
    assert (out / "results.json").exists()
    assert (out / "compare.md").exists()
    assert (out / "leaderboard-row.md").exists()
    payload = json.loads((out / "results.json").read_text())
    assert payload["trials"] == 1
    assert payload["groups"][0]["published_context"] == "0/111 published"


def test_cli_reports_an_empty_tree(tmp_path: Path) -> None:
    empty = tmp_path / "runs"
    empty.mkdir()
    assert compare.main([str(empty), "--out", str(tmp_path / "reports")]) == 1
    assert compare.main([str(tmp_path / "missing")]) == 2


def test_a_trial_missing_its_sidecars_still_produces_a_row(tmp_path: Path) -> None:
    trial_dir = tmp_path / "widget-review" / "baseline" / "20260913T000000-r1"
    trial_dir.mkdir(parents=True)
    (trial_dir / "verdict.json").write_text(json.dumps({"outcome": "fail"}))
    rows = compare.collect(tmp_path)
    assert len(rows) == 1
    assert rows[0].agent == "baseline"
    assert rows[0].arm == "baseline"
    assert rows[0].scenario == "widget-review"
    assert rows[0].provider_calls == 0
