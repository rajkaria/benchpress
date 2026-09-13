"""Policy packs: schema, loading, gate application, corpus coverage, CLI, and run_trial wiring."""

from __future__ import annotations

from pathlib import Path

import pytest

from benchpress.cli import main
from benchpress.context import Context, DefinitionOfDone
from benchpress.gate import Gate, GateRefusal
from benchpress.gate_corpus import CaseAction, load_corpus
from benchpress.packs import (
    PolicyPack,
    PolicyPackError,
    available_policy_packs,
    load_policy_pack,
    load_policy_packs,
)

BUNDLED = ("billing", "customer-success", "it-offboarding", "release-engineering")
CASES = load_corpus()


def _action(**fields: object) -> CaseAction:
    return CaseAction.model_validate(fields)


def test_bundled_packs_load() -> None:
    assert available_policy_packs() == BUNDLED
    for name in BUNDLED:
        pack = load_policy_pack(name)
        assert pack.name == name and pack.rules and pack.source.endswith(f"{name}.yaml")
        assert all(rule.id.startswith(f"{name}.") for rule in pack.rules)


@pytest.mark.parametrize("name", BUNDLED)
def test_every_pack_rule_has_a_corpus_refusal(name: str) -> None:
    proven = {
        case.expect.rule
        for case in CASES
        if case.expect.decision == "refuse" and not case.xfail and name in case.packs
    }
    missing = [rule.id for rule in load_policy_pack(name).rules if f"pack:{rule.id}" not in proven]
    assert not missing, f"pack rules without a corpus refusal case: {missing}"


def test_packs_only_add_refusals() -> None:
    """Across the whole corpus, anything all four packs allow, the default gate allows too."""
    packs = load_policy_packs(BUNDLED)
    for case in CASES:
        action = case.action.to_action()
        default = Gate(case.context.build(), allow_unplanned=not case.context.enforce_plan)
        packed = Gate(case.context.build(), allow_unplanned=not case.context.enforce_plan, policy_packs=packs)
        if packed.evaluate(action).allowed:
            assert default.evaluate(action).allowed, case.name


def test_gate_without_packs_is_unchanged() -> None:
    for case in CASES:
        if case.packs:
            continue
        action = case.action.to_action()
        plain = Gate(case.context.build(), allow_unplanned=not case.context.enforce_plan)
        empty = Gate(case.context.build(), allow_unplanned=not case.context.enforce_plan, policy_packs=())
        assert plain.evaluate(action) == empty.evaluate(action), case.name


def test_pack_refusal_is_recorded_with_its_rule_id() -> None:
    ctx = Context(user_prompt="internal team at corp.example")
    gate = Gate(ctx, allow_unplanned=True, policy_packs=(load_policy_pack("billing"),))
    action = _action(provider="stripe", method="POST", path="/v1/refunds", body={"charge": "ch_1"}).to_action()
    with pytest.raises(GateRefusal) as excinfo:
        gate.check(action)
    assert excinfo.value.rule == "pack:billing.refund-requires-approval"
    assert ctx.refusals[-1].rule == "pack:billing.refund-requires-approval"
    assert "billing.refund-requires-approval" in ctx.refusals[-1].reason
    assert "refund_approval" in ctx.refusals[-1].reason


@pytest.mark.parametrize(("value", "allowed"), [("approved", True), ("YES", True), ("pending", False), ("", False)])
def test_approval_fact_values(value: str, allowed: bool) -> None:
    ctx = Context(dod=DefinitionOfDone(facts={"Refund_Approval": value}))
    gate = Gate(ctx, allow_unplanned=True, policy_packs=(load_policy_pack("billing"),))
    action = _action(provider="stripe", method="POST", path="/v1/refunds", body={"charge": "ch_1"}).to_action()
    assert gate.evaluate(action).allowed is allowed


def _pack(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "name": "local",
        "title": "Local",
        "rules": [{"id": "local.no-crm", "reason": "CRM is read-only", "match": {"providers": ["hubspot"]}}],
    }
    data.update(overrides)
    return data


@pytest.mark.parametrize(
    "rules",
    [
        [{"id": "other.no-crm", "reason": "r", "match": {"providers": ["hubspot"]}}],
        [{"id": "local.empty", "reason": "r", "match": {}}],
        [{"id": "local.bad-regex", "reason": "r", "match": {"path": "("}}],
        [{"id": "local.bad-class", "reason": "r", "match": {"action_classes": ["launch_rockets"]}}],
        [{"id": "local.x", "reason": "r", "match": {"methods": ["GET"]}}],
        [
            {"id": "local.x", "reason": "r", "match": {"providers": ["a"]}},
            {"id": "local.x", "reason": "r", "match": {"providers": ["b"]}},
        ],
        [],
    ],
)
def test_pack_schema_rejects(rules: list[object]) -> None:
    with pytest.raises(ValueError):
        PolicyPack.model_validate(_pack(rules=rules))


def test_load_pack_from_path_and_errors(tmp_path: Path) -> None:
    path = tmp_path / "local.yaml"
    path.write_text(
        "name: local\ntitle: Local\nrules:\n  - {id: local.no-crm, reason: r, match: {providers: [hubspot]}}\n"
    )
    assert load_policy_pack(path).name == "local"
    with pytest.raises(PolicyPackError, match="bundled packs"):
        load_policy_pack("no-such-pack")
    with pytest.raises(PolicyPackError, match="twice"):
        load_policy_packs(["billing", "billing"])
    with pytest.raises(PolicyPackError):
        load_policy_pack(tmp_path / "missing.yaml")


def test_corpus_case_can_use_a_pack_file_next_to_it(tmp_path: Path) -> None:
    (tmp_path / "local.yaml").write_text(
        "name: local\ntitle: Local\nrules:\n  - {id: local.no-crm, reason: r, match: {providers: [hubspot]}}\n"
    )
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    (cases_dir / "c.yaml").write_text(
        "cases:\n"
        "  - name: local-crm-write-refused\n"
        "    packs: [../local.yaml]\n"
        "    action: {provider: hubspot, method: PATCH, path: /crm/v3/objects/companies/1}\n"
        '    expect: {decision: refuse, rule: "pack:local.no-crm"}\n'
    )
    assert main(["gate", "check", str(cases_dir)]) == 0


def test_case_expecting_a_pack_rule_must_name_the_pack(tmp_path: Path) -> None:
    path = tmp_path / "c.yaml"
    path.write_text(
        "cases:\n"
        "  - name: x\n"
        "    action: {provider: stripe, method: POST, path: /v1/refunds}\n"
        '    expect: {decision: refuse, rule: "pack:billing.refund-requires-approval"}\n'
    )
    assert main(["gate", "check", str(path)]) == 2


def test_cli_policy_list_and_show(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["policy", "list"]) == 0
    listing = capsys.readouterr().out
    assert all(name in listing for name in BUNDLED)
    assert main(["policy", "show", "billing"]) == 0
    shown = capsys.readouterr().out
    assert all(rule.id in shown for rule in load_policy_pack("billing").rules)
    assert "unless approved by fact: refund_approval" in shown
    assert main(["policy", "show", "nope"]) == 2


@pytest.mark.asyncio
async def test_run_trial_applies_policy_packs(tmp_path: Path) -> None:
    import json

    from benchpress.controller import run_trial
    from benchpress.model import ModelClient, ModelConfig
    from tests.test_controller import (
        PROMPT,
        GmailBook,
        HubSpotBook,
        ScriptedTransport,
        SlackBook,
        StripeBook,
        Workspace,
    )

    pack = PolicyPack.model_validate(_pack())
    workspace = Workspace()
    transport = ScriptedTransport()
    result = await run_trial(
        system_prompt="harness prompt",
        user_prompt=PROMPT,
        providers=["slack", "gmail", "hubspot", "stripe"],
        execute_tool=workspace.execute_tool,
        config=ModelConfig(model="scripted"),
        model_client=ModelClient(ModelConfig(model="scripted"), "sys", transport=transport),
        playbooks={"slack": SlackBook(), "gmail": GmailBook(), "hubspot": HubSpotBook(), "stripe": StripeBook()},
        trial_id="t-packs",
        trace_dir=tmp_path,
        policy_packs=(pack,),
    )
    refusals = [verdict for verdict in result.context.refusals if verdict.rule == "pack:local.no-crm"]
    assert refusals and all("local.no-crm" in verdict.reason for verdict in refusals)
    crm_writes = [
        (call.get("method"), call.get("path"))
        for call in workspace.calls
        if call.get("provider") == "hubspot"
        and call.get("method") != "GET"
        and not str(call.get("path", "")).endswith("/search")  # POST search is a playbook read, not gated
    ]
    assert not crm_writes, crm_writes
    receipt = json.loads((tmp_path / "receipt.json").read_text())
    assert any(item["rule"] == "pack:local.no-crm" for item in receipt["refusals"])
