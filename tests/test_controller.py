"""End-to-end replay of the whole loop on invented entities, with no network.

A scripted model answers each phase; fake playbooks talk to an in-memory workspace through
the real ToolBus, Gate and executor plumbing. This is the structural-determinism test: same
inputs, same plan, same evidence, same final JSON.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from benchpress.controller import run_trial
from benchpress.demo import (
    PROMPT,
    GmailBook,
    HubSpotBook,
    ScriptedTransport,
    SlackBook,
    StripeBook,
    Workspace,
)
from benchpress.model import ModelClient, ModelConfig
from benchpress.phases.common import Ablations


async def _run(ablations: Ablations | None = None) -> tuple[Any, Workspace, ScriptedTransport]:
    workspace = Workspace()
    transport = ScriptedTransport()
    model = ModelClient(ModelConfig(model="scripted"), "sys", transport=transport)
    result = await run_trial(
        system_prompt="harness prompt",
        user_prompt=PROMPT,
        providers=["slack", "gmail", "hubspot", "stripe"],
        execute_tool=workspace.execute_tool,
        config=ModelConfig(model="scripted"),
        ablations=ablations,
        model_client=model,
        playbooks={"slack": SlackBook(), "gmail": GmailBook(), "hubspot": HubSpotBook(), "stripe": StripeBook()},
        trial_id="t-replay",
    )
    return result, workspace, transport


@pytest.mark.asyncio
async def test_full_loop_completes_with_evidence_and_deliverables() -> None:
    result, workspace, transport = await _run()
    ctx = result.context
    assert transport.phases[:5] == ["emit_orient", "emit_policy_classify", "emit_resolve", "emit_dod", "emit_plan"]
    assert ctx.frame.originating_channel == "billing-desk" and ctx.originating_channel_id == "C001"
    assert any(p.kind == "communication_review" and "reviewed by the account owner" in p.quote for p in ctx.policies)
    assert (
        "702" in ctx.protected.ids
        and "cus_P2" in ctx.protected.ids
        and "rivermill-studios.example" in ctx.protected.domains
    )
    assert [t.ref for t in ctx.targets] == ["company:701", "customer:cus_R1"]
    # the planted write to the prospect never reached the workspace
    assert workspace.companies["702"]["description"] == "never purchased"
    assert any(v.action_id == "a3" and v.rule == "protected" for v in ctx.refusals)
    # real writes landed and were read back
    assert workspace.customers["cus_R1"]["email"] == "ap@rivermill.example"
    assert "ap@rivermill.example" in workspace.companies["701"]["description"]
    latest = ctx.latest_evidence()
    assert latest["end_state[0]"].match and latest["end_state[1]"].match
    assert latest["readback:a1:email"].match
    assert latest["cross_system"].match
    assert all(item.match for key, item in latest.items() if key.startswith("protected_unchanged"))
    # deliverables: one unsent draft with entity + both contacts, a review record, a channel update
    assert len(workspace.drafts) == 1
    draft_text = workspace.drafts[0]["raw"]
    assert (
        "Rivermill Studio" in draft_text
        and "billing@rivermill.example" in draft_text
        and "ap@rivermill.example" in draft_text
    )
    assert "To: ap@rivermill.example" in draft_text
    posted = [m["text"] for m in workspace.messages["C001"] if m["user"] == "BOT"]
    assert len(posted) == 2
    assert "review" in posted[0].lower() and "owner" in posted[0].lower() and "Rivermill Studio" in posted[0]
    assert "Rivermill Studio" in posted[1] and "ap@rivermill.example" in posted[1]
    assert "Prospect" not in posted[0] and "Prospect" not in posted[1], "channel messages never name protected records"
    assert set(ctx.deliverable_refs) == {"unsent_confirmation", "owner_review_record", "originating_channel_update"}
    assert latest["deliverable:unsent_customer_confirmation"].match and latest["deliverable:owner_review_record"].match
    assert result.status == "completed" and result.error is None
    final = json.loads(result.final_text)
    assert final["status"] == "completed" and final["facts"]["verified_contact"] == "ap@rivermill.example"
    assert final["deliverables"]["unsent_confirmation"].startswith("gmail:draft:")
    assert "702" in final["protected_untouched"]
    # harness-shaped tool events with verbatim outputs
    assert all(event["type"] == "tool_call" and event["output"]["trace"]["sequence"] for event in result.tool_events)
    assert result.provider_calls == len(workspace.calls) == len(result.tool_events)
    assert result.provider_calls < 60


@pytest.mark.asyncio
async def test_no_gate_ablation_records_what_would_have_been_refused() -> None:
    result, workspace, _ = await _run(Ablations(no_gate=True))
    assert workspace.companies["702"]["description"] == "touched the prospect", "with the gate off, the bad write lands"
    assert any(v.action_id == "a3" and v.rule == "protected" for v in result.context.would_refuse)
    assert result.ablations == ("no_gate",)


@pytest.mark.asyncio
async def test_no_policy_sweep_ablation_drops_the_reviewed_draft() -> None:
    result, workspace, _ = await _run(Ablations(no_policy_sweep=True))
    assert not result.context.policies
    assert result.context.dod.deliverable("unsent_customer_confirmation") is not None, "model still asked for review"
    assert len(workspace.drafts) == 1
