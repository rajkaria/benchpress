"""Replay: a converged plan executed with no model, through the gate, read back write by write."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, cast

from benchpress.context import Action
from benchpress.demo import SCRIPT
from benchpress.rehearse import (
    Rehearsal,
    record_difference,
    replay,
    substitute_action,
)
from tests.test_rehearse import OffsetWorkspace, WorkspaceStage, convergent_script, run_rehearsal


async def converged() -> Rehearsal:
    result, _ = await run_rehearsal([convergent_script()])
    assert result.converged, result.summary()
    return result


def test_substitute_action_rewrites_ids_created_earlier_in_the_plan() -> None:
    action = Action(
        id="c1",
        kind="comment",
        provider="tracker",
        method="POST",
        path="/items/itm_0042/comments",
        body={"parent": "itm_0042", "text": "follow-up to itm_0042; not xitm_0042"},
        headers={"Idempotency-Key": "bp-c1"},
    )
    updated, used = substitute_action(action, {"itm_0042": "itm_7777"})
    assert updated.path == "/items/itm_7777/comments"
    assert updated.body == {"parent": "itm_7777", "text": "follow-up to itm_7777; not xitm_0042"}
    assert used == {"itm_0042": "itm_7777"} and updated.headers == {"Idempotency-Key": "bp-c1"}
    same, nothing = substitute_action(action, {})
    assert same is action and nothing == {}


def test_record_difference_treats_unbound_placeholders_as_wildcards() -> None:
    reference = {"id": "<id:3>", "name": "kept", "parent": "<id:1>"}
    assert (
        record_difference(reference, {"id": "anything", "name": "kept", "parent": "<id:1>"}, frozenset({"<id:1>"}))
        is None
    )
    assert (
        record_difference(reference, {"id": "x", "name": "kept", "parent": "<id:9>"}, frozenset({"<id:1>"})) == "parent"
    )
    assert record_difference(reference, {"id": "x", "name": "changed", "parent": "<id:1>"}, frozenset()) == "name"


async def test_replay_on_a_fresh_stage_reproduces_the_rehearsed_state() -> None:
    rehearsal = await converged()
    target = WorkspaceStage(OffsetWorkspace())
    receipt = await replay(rehearsal, target.execute_tool, snapshot=target.snapshot)
    assert receipt.status == "completed", receipt.summary()
    assert receipt.executed == receipt.planned == len(rehearsal.plan) and receipt.stopped_at is None
    assert all(step.ok and step.readback_checked and not step.mismatches for step in receipt.steps)
    assert receipt.final_state_hash == rehearsal.state_hash and receipt.state_matches_rehearsal is True
    # no model, no exploration: only the planned writes and their read-backs reached the provider
    writes = [
        call for call in target.workspace.calls if call["method"] != "GET" and not call["path"].endswith("/search")
    ]
    assert len(writes) == len(rehearsal.plan)
    assert len(target.workspace.calls) == len(rehearsal.plan) + sum(
        1 for step in receipt.steps if step.readback_checked
    )
    assert target.workspace.customers["cus_R1"]["email"] == "ap@rivermill.example"
    assert "COMPLETED" in receipt.summary()


async def test_replay_binds_ids_the_target_generates_differently() -> None:
    rehearsal = await converged()
    target = WorkspaceStage(OffsetWorkspace(offset=40))
    receipt = await replay(rehearsal, target.execute_tool, snapshot=target.snapshot)
    assert receipt.status == "completed", receipt.summary()
    assert receipt.id_map and receipt.id_map != {key: key for key in receipt.id_map}
    assert any(value == "d41" for value in receipt.id_map.values()), receipt.id_map
    assert receipt.state_matches_rehearsal is True, "normalized state still matches with different concrete ids"


async def test_replay_stops_at_the_first_mismatch_when_state_changed_since_rehearsal(tmp_path: Path) -> None:
    rehearsal = Rehearsal.read_json((await converged()).write_json(tmp_path / "rehearsal.json"))
    target = WorkspaceStage(OffsetWorkspace())
    target.workspace.companies["701"]["lifecyclestage"] = "churned"  # someone edited the record after rehearsal
    receipt = await replay(rehearsal, target.execute_tool, snapshot=target.snapshot)
    assert receipt.status == "stopped" and receipt.stopped_at == 1
    assert [step.action_id for step in receipt.steps] == ["a1", "a2"]
    assert receipt.steps[0].ok and not receipt.steps[0].mismatches
    assert receipt.steps[1].mismatches == ("record differs from the rehearsed record at properties.lifecyclestage",)
    assert "a2 landed but does not match the rehearsal" in receipt.reason
    assert receipt.executed == 2, "honest: the mismatching write did reach the provider"
    assert target.workspace.drafts == [], "nothing after the mismatch was attempted"
    assert not [m for m in target.workspace.messages["C001"] if m["user"] == "BOT"]
    assert receipt.state_matches_rehearsal is False
    assert "were not attempted" in receipt.summary()


async def test_replay_refuses_a_rehearsal_that_did_not_converge() -> None:
    diverged, _ = await run_rehearsal([SCRIPT])
    assert not diverged.converged
    target = WorkspaceStage(OffsetWorkspace())
    receipt = await replay(diverged, target.execute_tool)
    assert receipt.status == "refused" and "did not converge" in receipt.reason
    assert target.workspace.calls == []


async def test_the_gate_still_refuses_a_tampered_plan() -> None:
    rehearsal = await converged()
    tampered_write = rehearsal.plan[1].model_copy(
        update={"action": rehearsal.plan[1].action.model_copy(update={"path": "/crm/v3/objects/companies/702"})}
    )
    tampered = rehearsal.model_copy(update={"plan": (rehearsal.plan[0], tampered_write, *rehearsal.plan[2:])})
    target = WorkspaceStage(OffsetWorkspace())
    receipt = await replay(tampered, target.execute_tool)
    assert receipt.status == "stopped" and receipt.stopped_at == 1
    assert receipt.steps[1].allowed is False and receipt.steps[1].rule == "protected"
    assert receipt.executed == 1
    assert target.workspace.companies["702"]["description"] == "never purchased"


class ForgetfulWorkspace(OffsetWorkspace):
    """Accepts company writes, but its read API never returns `description`."""

    def _route(self, provider: str, method: str, path: str, body: dict[str, Any], query: dict[str, str]) -> object:
        result = super()._route(provider, method, path, body, query)
        if provider != "hubspot" or method != "GET" or not isinstance(result, dict):
            return result
        shown = copy.deepcopy(cast(dict[str, Any], result))
        properties = shown.get("properties")
        if isinstance(properties, dict):
            cast(dict[str, Any], properties).pop("description", None)
        return shown


async def test_replay_flags_a_written_field_the_readback_does_not_show() -> None:
    rehearsal = await converged()
    target = WorkspaceStage(ForgetfulWorkspace())
    receipt = await replay(rehearsal, target.execute_tool, snapshot=target.snapshot)
    missing = [m for step in receipt.steps for m in step.mismatches if m.startswith("description: wrote")]
    assert missing and missing[0].endswith("read-back does not show it"), receipt.summary()
    assert receipt.status == "stopped"
