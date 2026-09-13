"""Gate replay: which recorded writes the gate would refuse, and how honestly it says so."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "arga-twins-benchmark" / "tests" / "fixtures" / "argabench_crm_legacy"
FIXTURE_TARBALL = FIXTURE / "historical-fable-5-high-crm.tar.gz"


def _load_script() -> Any:
    spec = importlib.util.spec_from_file_location("bp_gate_replay", REPO_ROOT / "scripts" / "bp_gate_replay.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["bp_gate_replay"] = module
    spec.loader.exec_module(module)
    return module


replay_script = _load_script()


def tool_call(provider: str, method: str, path: str, body: object = None, name: str = "provider_api") -> dict[str, Any]:
    arguments: dict[str, Any] = {"provider": provider, "method": method, "path": path}
    if body is not None:
        arguments["body"] = body
    return {"type": "tool_call", "name": name, "arguments": arguments, "output": {"ok": True}}


# ------------------------------------------------------------------------------ event selection


def test_only_mutating_provider_api_calls_are_replayed() -> None:
    invocation = {
        "events": [
            {"type": "invocation_started"},
            tool_call("hubspot", "GET", "/crm/v3/objects/companies/1"),
            tool_call("hubspot", "PATCH", "/crm/v3/objects/companies/1", {"properties": {"x": "y"}}),
            tool_call("hubspot", "POST", "/crm/v3/objects/companies/search", {"filters": []}),
            tool_call("hubspot", "POST", "/docs", name="provider_docs"),
        ]
    }
    writes, skipped = replay_script.write_events(invocation)
    assert [item["method"] for item in writes] == ["PATCH"]
    assert skipped == 1


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/crm/v3/objects/deals/search", True),
        ("/services/data/v59.0/query?q=SELECT+Id", True),
        ("/crm/v3/objects/companies/batch/read", True),
        ("/crm/v3/objects/companies/701", False),
        ("/api/chat.postMessage", False),
    ],
)
def test_read_shaped_posts_are_recognised(path: str, expected: bool) -> None:
    assert replay_script.is_read_shaped(path) is expected


def test_provider_roles_resolve_to_provider_names() -> None:
    action = replay_script.action_from_arguments(
        1, {"provider": "hubspot_crm", "method": "POST", "path": "/x"}, {"hubspot_crm": "hubspot"}
    )
    assert action.provider == "hubspot"


def test_declared_fields_come_from_the_recorded_body() -> None:
    action = replay_script.action_from_arguments(
        1,
        {"provider": "slack", "method": "POST", "path": "/api/chat.postMessage", "query": {"limit": [1, 2]}},
    )
    assert action.query == {"limit": "1,2"}
    assert action.fields == ()


# ------------------------------------------------------------------------------ baseline replay


def receipt_payload(context: Any) -> dict[str, Any]:
    from benchpress.report import receipt_payload as build

    return build(context, meta={"trial": "t"})


def test_a_baseline_write_to_a_protected_record_is_refused(context: Any) -> None:
    invocation = {
        "events": [
            tool_call("hubspot", "PATCH", "/crm/v3/objects/companies/702", {"properties": {"description": "x"}}),
            tool_call("hubspot", "PATCH", "/crm/v3/objects/companies/701", {"properties": {"description": "ok"}}),
        ]
    }
    report = replay_script.replay(
        invocation,
        replay_script.context_from_receipt(receipt_payload(context)),
        label="baseline r1",
        source="runs/x",
        context_source="runs/y/receipt.json",
    )
    assert report.writes == 2
    refused = report.refused
    assert len(refused) == 1
    assert refused[0].rule == "protected"
    assert "702" in refused[0].path


def test_a_write_outside_the_authorised_scope_is_refused(context: Any) -> None:
    context.dod = context.dod.model_copy(update={"write_scope": ("hubspot",)})
    invocation = {"events": [tool_call("slack", "POST", "/api/chat.postMessage", {"text": "hello"})]}
    report = replay_script.replay(
        invocation,
        replay_script.context_from_receipt(receipt_payload(context)),
        label="baseline",
        source="s",
        context_source="c",
    )
    assert report.refused[0].rule == "provider_scope"


def test_receipt_rebuilds_the_deny_list_and_the_write_scope(context: Any) -> None:
    rebuilt = replay_script.context_from_receipt(receipt_payload(context))
    assert "702" in rebuilt.protected.ids
    assert rebuilt.dod.write_scope == context.dod.write_scope
    assert {target.resource_id for target in rebuilt.targets} == {"701", "cus_R1"}


def test_unplanned_writes_are_not_refused_for_being_unplanned(context: Any) -> None:
    """The baseline has no plan, so plan membership must not be what refuses it."""
    invocation = {"events": [tool_call("stripe", "POST", "/v1/customers/cus_R1", {"email": "x@rivermill.example"})]}
    report = replay_script.replay(
        invocation,
        replay_script.context_from_receipt(receipt_payload(context)),
        label="baseline",
        source="s",
        context_source="c",
    )
    assert all(call.rule != "plan_membership" for call in report.calls)


# ------------------------------------------------------------------------------ scoped rules


def test_rules_a_recording_cannot_support_are_reported_as_not_evaluable(context: Any) -> None:
    invocation = {"events": [tool_call("slack", "POST", "/api/chat.postMessage", {"text": "mail bob@elsewhere.test"})]}
    ctx = replay_script.context_from_receipt(receipt_payload(context))
    unscoped = replay_script.replay(invocation, ctx, label="l", source="s", context_source="c")
    assert unscoped.refused[0].rule == "external_destination"

    scoped = replay_script.replay(
        invocation,
        replay_script.context_from_receipt(receipt_payload(context)),
        label="l",
        source="s",
        context_source="c",
        scoped_rules=replay_script.SUITE_SCOPED_RULES,
    )
    assert scoped.refused == ()
    assert scoped.calls[0].rule == "not_evaluable:external_destination"


# ------------------------------------------------------------------------------ discovery + CLI


def test_find_receipt_prefers_the_trial_then_a_sibling_benchpress_arm(tmp_path: Path) -> None:
    scenario = tmp_path / "billing-review"
    baseline = scenario / "baseline" / "20260913T000000-r1"
    baseline.mkdir(parents=True)
    benchpress = scenario / "benchpress" / "20260913T000100-r1"
    benchpress.mkdir(parents=True)
    (benchpress / "receipt.json").write_text("{}")
    assert replay_script.find_receipt(baseline) == benchpress / "receipt.json"

    own = baseline / "receipt.json"
    own.write_text("{}")
    assert replay_script.find_receipt(baseline) == own


def test_a_trial_without_any_receipt_fails_loudly(tmp_path: Path) -> None:
    trial = tmp_path / "s" / "baseline" / "t"
    trial.mkdir(parents=True)
    (trial / "invocation.json").write_text(json.dumps({"events": []}))
    with pytest.raises(FileNotFoundError):
        replay_script.replay_trial_dir(trial)


def test_cli_writes_the_markdown_and_json_with_the_caveat(tmp_path: Path, context: Any) -> None:
    trial = tmp_path / "billing-review" / "baseline" / "20260913T000000-r1"
    trial.mkdir(parents=True)
    (trial / "invocation.json").write_text(
        json.dumps({"events": [tool_call("hubspot", "PATCH", "/crm/v3/objects/companies/702", {"x": 1})]})
    )
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps(receipt_payload(context)))
    out = tmp_path / "reports"

    assert replay_script.main([str(trial), "--receipt", str(receipt), "--out", str(out)]) == 0
    text = (out / "gate-replay.md").read_text()
    assert "would have refused" in text or "would refuse" in text
    assert "It does not mean the trial would have passed" in text
    payload = json.loads((out / "gate-replay.json").read_text())
    assert payload["reports"][0]["refused"] == 1
    assert payload["caveat"] == replay_script.CAVEAT


def test_cli_requires_a_target() -> None:
    assert replay_script.main([]) == 2


# ------------------------------------------------------------------------------ the real fixture


@pytest.mark.skipif(not FIXTURE_TARBALL.exists(), reason="vendored ArgaBench fixture is not present")
def test_historical_fixture_replays_against_suite_json_contexts(tmp_path: Path) -> None:
    reports = replay_script.replay_fixture(FIXTURE_TARBALL)
    assert len(reports) >= 5
    assert all(report.context_source.startswith("suite.json:") for report in reports)
    # Only the rules suite.json supports may ever appear as an actual refusal.
    for report in reports:
        for call in report.refused:
            assert call.rule in replay_script.SUITE_SCOPED_RULES
    assert any(report.refused for report in reports), "the gate should catch at least one recorded write"

    text = replay_script.render(reports, title="t", preamble=["p"])
    assert "read-shaped POSTs skipped" in text
