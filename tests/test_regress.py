"""`benchpress regress`: a run's gate decisions become gate-corpus cases that replay on the current gate."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from benchpress.cli import main
from benchpress.context import ProtectedSet
from benchpress.demo import run_demo
from benchpress.gate_corpus import CaseProtected, GateCase, check_command, load_corpus, run_case
from benchpress.regress import RegressResult, reduce_text, regress_receipt, write_cases


@pytest.fixture(scope="module")
def demo_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    trace_dir = tmp_path_factory.mktemp("demo")
    asyncio.run(run_demo(trace_dir))
    return trace_dir


@pytest.fixture(scope="module")
def regressed(demo_run: Path) -> RegressResult:
    return regress_receipt(demo_run / "receipt.json")


def _refused_lookalike(result: RegressResult) -> GateCase:
    refused = [item.case for item in result.cases if item.case.expect.decision == "refuse"]
    assert len(refused) == 1
    return refused[0]


def test_receipt_records_every_gate_decision_with_its_action(demo_run: Path) -> None:
    receipt: dict[str, Any] = json.loads((demo_run / "receipt.json").read_text())
    decisions = receipt["gate_decisions"]
    refused = [item for item in decisions if not item["verdict"]["allowed"]]
    assert [item["phase"] for item in refused] == ["P4"]
    assert refused[0]["action"]["path"].endswith("/702")
    assert all(item["at"].endswith("Z") for item in decisions)
    assert receipt["policy_packs"] == [] and receipt["generated_at"].endswith("Z")
    assert all(entry["at"] for entry in receipt["ledger"])


def test_every_regressed_case_passes_on_the_current_gate(regressed: RegressResult) -> None:
    assert not regressed.drift and not regressed.skipped
    assert len(regressed.cases) == 6  # one refused look-alike write + five allowed writes
    for item in regressed.cases:
        result = run_case(item.case)
        assert result.status == "pass", (item.case.name, result.actual)
        assert item.case.context.enforce_plan  # the run's gate enforces plan membership; so does the replay
        assert item.case.source.endswith(f"receipt.json#{item.case.action.id}")
        assert "regress" in item.case.tags


def test_the_refused_lookalike_write_is_a_case_with_its_rule(regressed: RegressResult) -> None:
    case = _refused_lookalike(regressed)
    assert case.expect.rule == "protected"
    assert case.action.provider == "hubspot" and case.action.path.endswith("/702")
    assert case.name.startswith("update-hubspot-refused-protected-")
    assert "702" in case.context.protected.ids


def test_ablating_the_protected_id_breaks_the_regressed_refusal(regressed: RegressResult) -> None:
    case = _refused_lookalike(regressed)
    protected = case.context.protected
    ablated_protected = CaseProtected(
        ids=tuple(item for item in protected.ids if item != "702"),
        names=protected.names,
        domains=protected.domains,
        emails=protected.emails,
    )
    ablated = case.model_copy(update={"context": case.context.model_copy(update={"protected": ablated_protected})})
    result = run_case(ablated)
    assert result.status == "fail" and result.verdict.allowed


def test_case_names_and_bodies_carry_no_record_prose(regressed: RegressResult, demo_run: Path) -> None:
    receipt: dict[str, Any] = json.loads((demo_run / "receipt.json").read_text())
    seeded = {str(target["display"]).casefold() for target in receipt["targets"]}
    seeded |= {str(name).casefold() for name in receipt["protected"]["names"]}
    for item in regressed.cases:
        assert item.redacted
        assert not any(name.replace(" ", "-") in item.case.name for name in seeded)
        body = json.dumps(item.case.action.body).casefold()
        assert "renewal notices" not in body and "review request" not in body
        assert item.case.context.prompt == "" or "@" not in item.case.context.prompt.replace(".example", "")


def test_redaction_keeps_the_tokens_a_decision_rests_on() -> None:
    protected = ProtectedSet(ids={"cus_P2"}, names={"Larkspur Goods Prospect"}, domains={"larkspur-goods.example"})
    text = "Moved from Larkspur Goods Prospect; write to ap@larkspur.example, see https://files.example/x.pdf"
    reduced = reduce_text(text, protected)
    assert reduced.startswith("redacted ")
    for token in ("ap@larkspur.example", "files.example", "Larkspur Goods Prospect"):
        assert token in reduced
    assert "Moved" not in reduced


def test_written_cases_replay_through_gate_check_and_the_cli(
    regressed: RegressResult, demo_run: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = write_cases(regressed, tmp_path / "cases", "demo")
    assert out.suffix == ".yaml"
    loaded = load_corpus([out.parent])
    assert {case.name for case in loaded} == {item.case.name for item in regressed.cases}
    assert check_command([str(out.parent)]) == 0
    capsys.readouterr()
    assert main(["regress", str(demo_run), "--out", str(tmp_path / "cli")]) == 0
    printed = capsys.readouterr().out
    assert "refuse:protected" in printed and "0 drift" in printed
    assert main(["gate", "check", str(tmp_path / "cli")]) == 0


def test_legacy_receipt_without_gate_decisions_rebuilds_allowed_writes(demo_run: Path, tmp_path: Path) -> None:
    receipt: dict[str, Any] = json.loads((demo_run / "receipt.json").read_text())
    receipt.pop("gate_decisions")
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "receipt.json").write_text(json.dumps(receipt))
    (legacy / "benchpress-trace.jsonl").write_text((demo_run / "benchpress-trace.jsonl").read_text())
    result = regress_receipt(legacy)
    assert len(result.cases) == 5 and all(item.case.expect.decision == "allow" for item in result.cases)
    assert all(run_case(item.case).status == "pass" for item in result.cases)
    assert any("a3" in note and "protected" in note for note in result.skipped)


def test_a_decision_the_gate_no_longer_makes_is_reported_as_drift(
    demo_run: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    receipt: dict[str, Any] = json.loads((demo_run / "receipt.json").read_text())
    for item in receipt["gate_decisions"]:
        if item["action"]["id"] == "a1":
            item["verdict"] = {"action_id": "a1", "allowed": False, "rule": "external_destination", "reason": "x"}
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(receipt))
    result = regress_receipt(tampered)
    assert [drift.decision.action.id for drift in result.drift] == ["a1"]
    assert main(["regress", str(tampered), "--out", str(tmp_path / "out")]) == 1
    assert "DRIFT a1" in capsys.readouterr().out
    written = yaml.safe_load(next((tmp_path / "out").glob("*.yaml")).read_text())
    assert len(written["cases"]) == 5


def test_unreadable_receipt_is_a_usage_error(tmp_path: Path) -> None:
    assert main(["regress", str(tmp_path / "missing.json"), "--out", str(tmp_path / "out")]) == 2
