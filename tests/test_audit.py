"""`benchpress receipts export`: one audit row per write attempt, stable columns, names not values."""

from __future__ import annotations

import asyncio
import csv
import io
import json
from pathlib import Path
from typing import Any, cast

import pytest

from benchpress.audit import AUDIT_COLUMNS, export_rows, find_receipts, receipt_rows, render_csv, render_jsonl
from benchpress.cli import main
from benchpress.demo import run_demo


@pytest.fixture(scope="module")
def runs(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("runs")
    asyncio.run(run_demo(root / "first", trial_id="audit-first"))
    asyncio.run(run_demo(root / "nested" / "second", trial_id="audit-second"))
    return root


def _receipt(runs: Path) -> dict[str, Any]:
    return json.loads((runs / "first" / "receipt.json").read_text())


def _by_action(rows: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    return {str(row["action_id"]): row for row in rows}


def test_one_row_per_write_attempt_in_judgement_order(runs: Path) -> None:
    rows = receipt_rows(_receipt(runs), "first/receipt.json")
    assert [row["action_id"] for row in rows] == ["a3", "a1", "a2", "d-draft", "d-review", "d-update"]
    assert all(tuple(row) == AUDIT_COLUMNS for row in rows)
    assert {row["trial_id"] for row in rows} == {"audit-first"}
    assert {row["final_status"] for row in rows} == {"completed"}
    assert all(str(row["timestamp"]).endswith("Z") for row in rows)


def test_refused_write_carries_rule_and_never_executed(runs: Path) -> None:
    refused = _by_action(receipt_rows(_receipt(runs)))["a3"]
    assert refused["gate_decision"] == "refuse" and refused["gate_rule"] == "protected"
    assert "702" in str(refused["gate_reason"]) and refused["phase"] == "P4"
    assert refused["executed"] is False and refused["readback"] == "not_executed"
    assert refused["status_code"] is None and refused["sequence"] is None


def test_allowed_update_names_fields_evidence_and_dod_item_not_values(runs: Path) -> None:
    row = _by_action(receipt_rows(_receipt(runs)))["a1"]
    assert row["provider"] == "stripe" and row["method"] == "POST" and row["action_kind"] == "update"
    assert row["gate_decision"] == "allow" and row["gate_rule"] == ""
    assert row["executed"] is True and row["status_code"] == 200 and row["provider_ok"] is True
    assert row["fields_changed"] == ["email"]
    assert row["readback"] == "match" and row["readback_checks"] == ["readback:a1:email=match"]
    assert row["dod_items"] == ["end_state[0]: stripe customer:cus_R1 email"]
    assert row["dod_evidence"] == ["end_state[0]=match"]
    assert row["requested_by"] == "Dana Reyes" and row["agent_model"] == "scripted"
    assert "@" not in json.dumps({key: row[key] for key in ("fields_changed", "dod_items", "readback_checks")})


def test_policy_governed_deliverables_quote_the_policy(runs: Path) -> None:
    rows = _by_action(receipt_rows(_receipt(runs)))
    for action_id in ("d-draft", "d-review"):
        policies = cast(list[str], rows[action_id]["policies"])
        assert len(policies) == 1
        assert "account owner" in policies[0]
        assert "[communication_review] policy:gmail:message:m2" in policies[0]
    assert rows["a1"]["policies"] == []


def test_export_walks_directories_and_both_formats_keep_the_column_order(runs: Path) -> None:
    assert len(find_receipts([runs, runs / "first" / "receipt.json"])) == 2
    rows = export_rows([runs])
    assert len(rows) == 12 and {row["trial_id"] for row in rows} == {"audit-first", "audit-second"}
    parsed = list(csv.reader(io.StringIO(render_csv(rows))))
    assert tuple(parsed[0]) == AUDIT_COLUMNS and len(parsed) == 13
    first = dict(zip(AUDIT_COLUMNS, parsed[2], strict=True))
    assert first["executed"] == "true" and first["fields_changed"] == "email"
    lines = [json.loads(line) for line in render_jsonl(rows).splitlines()]
    assert all(tuple(line) == AUDIT_COLUMNS for line in lines)


def test_cli_export_writes_a_file_and_rejects_missing_paths(runs: Path, tmp_path: Path) -> None:
    out = tmp_path / "audit" / "log.csv"
    assert main(["receipts", "export", str(runs), "--format", "csv", "--out", str(out)]) == 0
    assert out.read_text().splitlines()[0] == ",".join(AUDIT_COLUMNS)
    jsonl = tmp_path / "log.jsonl"
    assert main(["receipts", "export", str(runs / "first"), "--out", str(jsonl)]) == 0
    assert len(jsonl.read_text().splitlines()) == 6
    assert main(["receipts", "export", str(tmp_path / "missing")]) == 2


def test_non_receipt_json_is_rejected(tmp_path: Path) -> None:
    other = tmp_path / "receipt.json"
    other.write_text(json.dumps({"protocol": "something-else"}))
    assert main(["receipts", "export", str(other)]) == 2


def test_legacy_receipt_without_gate_decisions_still_exports(runs: Path) -> None:
    receipt = _receipt(runs)
    receipt.pop("gate_decisions")
    rows = _by_action(receipt_rows(receipt))
    assert set(rows) == {"a1", "a2", "d-draft", "d-review", "d-update", "a3"}
    assert rows["a3"]["gate_rule"] == "protected" and rows["a3"]["executed"] is False
    assert rows["a1"]["readback"] == "match" and rows["a1"]["fields_changed"] == ["email"]
