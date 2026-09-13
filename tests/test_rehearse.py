"""Rehearse: N runs on fresh stages, a convergence verdict, and a readable divergence report.

The stage is the in-memory workspace from tests/test_controller.py; the model is scripted. No
network, no keys. Names are invented.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from typing import Any, cast

import pytest

from benchpress.model import ModelClient, ModelConfig
from benchpress.playbooks import Playbook
from benchpress.rehearse import (
    DEFAULT_NORMALIZER,
    Normalizer,
    Rehearsal,
    Stage,
    first_difference,
    rehearse,
    state_hash,
)
from tests.test_controller import PROMPT, SCRIPT, GmailBook, HubSpotBook, SlackBook, StripeBook, Workspace

PROVIDERS = ["slack", "gmail", "hubspot", "stripe"]


def playbooks() -> dict[str, Playbook]:
    return {"slack": SlackBook(), "gmail": GmailBook(), "hubspot": HubSpotBook(), "stripe": StripeBook()}


# --------------------------------------------------------------------------------------
# Stage + scripted model
# --------------------------------------------------------------------------------------


class OffsetWorkspace(Workspace):
    """A workspace whose generated ids start at `offset`, so two stages can disagree on them."""

    def __init__(self, offset: int = 0) -> None:
        super().__init__()
        self.offset = offset
        self.next_ts = 2 + offset

    def _route(self, provider: str, method: str, path: str, body: dict[str, Any], query: dict[str, str]) -> object:
        if provider == "gmail" and path.endswith("/drafts") and method == "POST" and self.offset:
            result = cast(dict[str, Any], super()._route(provider, method, path, body, query))
            draft = self.drafts[-1]
            draft["id"] = f"d{self.offset + len(self.drafts)}"
            result["id"] = draft["id"]
            return result
        return super()._route(provider, method, path, body, query)


class WorkspaceStage:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.closed = False

    async def execute_tool(self, tool_name: str, tool_input: dict[str, Any]) -> object:
        return await self.workspace.execute_tool(tool_name, tool_input)

    async def snapshot(self) -> dict[str, Any]:
        ws = self.workspace
        return copy.deepcopy(
            {
                "hubspot": {"companies": ws.companies},
                "stripe": {"customers": ws.customers},
                "slack": {"channels": ws.channels, "messages": ws.messages},
                "gmail": {"mail": ws.mail, "drafts": ws.drafts},
            }
        )

    async def aclose(self) -> None:
        self.closed = True


def workspace_factory(offsets: list[int] | None = None, made: list[WorkspaceStage] | None = None) -> Callable[[], Any]:
    counter = {"n": 0}

    async def factory() -> Stage:
        index = counter["n"]
        counter["n"] += 1
        offset = offsets[index % len(offsets)] if offsets else 0
        stage = WorkspaceStage(OffsetWorkspace(offset))
        if made is not None:
            made.append(stage)
        return stage

    return factory


class Transport:
    def __init__(self, script: dict[str, dict[str, Any]]) -> None:
        self.script = script

    async def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        name = str(cast(dict[str, Any], payload["tool_choice"])["function"]["name"])
        call = {"id": "c1", "type": "function", "function": {"name": name, "arguments": json.dumps(self.script[name])}}
        return {
            "model": "scripted",
            "choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [call]}, "finish_reason": "x"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }

    async def aclose(self) -> None:
        return None


def convergent_script() -> dict[str, dict[str, Any]]:
    script = copy.deepcopy(SCRIPT)
    script["emit_plan"]["actions"] = [a for a in script["emit_plan"]["actions"] if a["id"] != "a3"]
    return script


def other_record_script() -> dict[str, dict[str, Any]]:
    """Same request, but this run's model resolves the look-alike company instead."""
    script = convergent_script()
    chosen = script["emit_resolve"]["chosen"]
    chosen[0] = {**chosen[0], "resource_id": "702", "display": "Rivermill Studios Prospect"}
    script["emit_resolve"]["near_duplicate_ids"] = ["701", "cus_P2"]
    script["emit_dod"]["end_state"][1]["resource"] = "company:702"
    script["emit_plan"]["actions"][1]["ref"] = "company:702"
    return script


def model_factory(scripts: list[dict[str, dict[str, Any]]]) -> Callable[[int], ModelClient]:
    def make(index: int) -> ModelClient:
        return ModelClient(ModelConfig(model="scripted"), "sys", transport=Transport(scripts[index % len(scripts)]))

    return make


async def run_rehearsal(
    scripts: list[dict[str, dict[str, Any]]], *, offsets: list[int] | None = None, n: int = 3
) -> tuple[Rehearsal, list[WorkspaceStage]]:
    made: list[WorkspaceStage] = []
    result = await rehearse(
        PROMPT,
        PROVIDERS,
        workspace_factory(offsets, made),
        n=n,
        model_factory=model_factory(scripts),
        playbooks=playbooks(),
    )
    return result, made


# --------------------------------------------------------------------------------------
# Normalizer
# --------------------------------------------------------------------------------------


def test_normalizer_collapses_timestamps_by_key_and_by_value() -> None:
    value = {
        "created": 1_788_000_000,
        "updatedAt": "whatever",
        "sent_at": 5,
        "properties": {"hs_lastmodifieddate": "2026-09-01T09:00:00.000Z", "note": "2026-09-01T09:00:00Z"},
        "due": "2026-10-01",
        "count": 3,
    }
    normalized = DEFAULT_NORMALIZER.normalize(value, {})
    assert normalized == {
        "count": 3,
        "created": "<time>",
        "due": "2026-10-01",
        "properties": {"hs_lastmodifieddate": "<time>", "note": "<time>"},
        "sent_at": "<time>",
        "updatedAt": "<time>",
    }


def test_generated_ids_are_renumbered_by_first_appearance() -> None:
    baseline = {"items": {"itm_0001": {"id": "itm_0001", "name": "kept"}}}
    response_a = {"id": "itm_0099", "ts": "17.000200"}
    final = {
        "items": {"itm_0001": {"id": "itm_0001"}, "itm_0099": {"id": "itm_0099", "parent_id": "itm_0001"}},
        "log": [{"ts": "17.000200", "text": "created itm_0099 from itm_0001"}, {"id": "evt_5"}],
    }
    generated = DEFAULT_NORMALIZER.generated_ids(baseline, response_a, final)
    assert generated == {"itm_0099": "<id:1>", "17.000200": "<id:2>", "evt_5": "<id:3>"}
    normalized = DEFAULT_NORMALIZER.normalize(final, generated)
    assert normalized["items"]["<id:1>"] == {"id": "<id:1>", "parent_id": "itm_0001"}
    assert normalized["log"][0] == {"text": "created <id:1> from itm_0001", "ts": "<id:2>"}
    # a token boundary is respected: an id embedded in a longer identifier is not rewritten
    assert DEFAULT_NORMALIZER.normalize("xitm_0099 itm_0099.", generated) == "xitm_0099 <id:1>."


def test_same_structure_with_different_generated_ids_hashes_equal() -> None:
    def world(new_id: str) -> dict[str, Any]:
        return {"records": {new_id: {"id": new_id, "created_at": new_id[-2:]}}, "seen": [f"see {new_id}"]}

    hashes: list[str] = []
    for new_id in ("rec_1111", "rec_9999"):
        generated = DEFAULT_NORMALIZER.generated_ids({}, world(new_id))
        hashes.append(state_hash(DEFAULT_NORMALIZER.normalize(world(new_id), generated)))
    assert hashes[0] == hashes[1]


def test_normalizer_rules_are_configurable() -> None:
    strict = Normalizer(time_keys=frozenset(), time_suffixes=())
    assert strict.normalize({"created": 5}, {}) == {"created": 5}
    assert strict.is_id_key("customer_id") and not strict.is_time_key("created_at")


def test_first_difference_names_the_path() -> None:
    assert first_difference({"a": {"b": [1, 2]}}, {"a": {"b": [1, 3]}}) == "a.b.1"
    assert first_difference({"a": 1}, {"a": 1, "z": 0}) == "z"
    assert first_difference([1], [1]) is None


# --------------------------------------------------------------------------------------
# rehearse()
# --------------------------------------------------------------------------------------


async def test_deterministic_model_converges_with_a_typed_plan() -> None:
    result, stages = await run_rehearsal([convergent_script()])
    assert result.converged, result.summary()
    assert result.reasons == () and result.divergence is None
    assert len(stages) == 3 and all(stage.closed for stage in stages), "every run gets its own stage, closed"
    assert len({run.state_hash for run in result.runs}) == 1 and result.state_hash == result.runs[0].state_hash
    ids = [write.action_id for write in result.plan]
    assert ids[:2] == ["a1", "a2"] and {"d-draft", "d-review", "d-update"} & set(ids)
    first = result.plan[0]
    assert first.action.provider == "stripe" and first.ok and len(first.fingerprint) == 32
    assert first.readback is not None and first.readback["email"] == "ap@rivermill.example"
    # generated ids (draft id, message ts) are renumbered, and the plan records where they came from
    assert any(write.created for write in result.plan)
    assert result.generated_ids and all(key.startswith("<id:") for key in result.generated_ids)
    assert result.gate_context["protected"]["ids"], "the gate context travels with the plan"
    # JSON round-trip
    again = Rehearsal.model_validate_json(result.model_dump_json())
    assert again == result


async def test_generated_ids_that_differ_between_stages_still_converge() -> None:
    result, _ = await run_rehearsal([convergent_script()], offsets=[0, 40, 7])
    assert result.converged, result.summary()
    concrete = [run.generated_ids for run in result.runs]
    assert concrete[0] != concrete[1], "the stages really did hand out different ids"


async def test_a_model_that_picks_different_records_diverges_with_a_readable_report() -> None:
    result, _ = await run_rehearsal([convergent_script(), other_record_script()])
    assert not result.converged
    assert result.plan == () and result.state_hash is None
    divergence = result.divergence
    assert divergence is not None and 1 in divergence.differing_runs and 0 not in divergence.differing_runs
    run1 = next(diff for diff in divergence.diffs if diff.run == 1)
    assert run1.state_path is not None and run1.first_write is not None
    # run 0 updated the customer; run 1 resolved the look-alike, so the controller escalated instead
    assert [w.action_id for w in result.runs[0].writes][:2] == ["a1", "a2"]
    assert result.runs[1].status == "escalated" and all(w.action_id != "a1" for w in result.runs[1].writes)
    assert run1.first_write.index == 0 and (run1.first_write.reference or "").startswith("#0 a1 stripe POST")
    report = result.summary()
    assert "DIVERGED" in report and f"run 1 vs run 0: final state differs at {run1.state_path}" in report
    assert f"run 1 vs run 0: first differing write #{run1.first_write.index}" in report
    assert all(diff.run != 2 for diff in divergence.diffs), "run 2 used the same script as run 0"


async def test_a_single_gate_refusal_blocks_convergence() -> None:
    result, _ = await run_rehearsal([SCRIPT])  # the stock script plans a write to the protected prospect
    assert len({run.state_hash for run in result.runs}) == 1, "the refused write never landed"
    assert not result.converged
    assert any("refused" in reason for reason in result.reasons)
    assert result.divergence is not None
    assert {note.rule for note in result.divergence.refusals} == {"protected"}
    assert "gate refused a3 (protected" in result.summary()


async def test_rehearse_needs_at_least_two_runs() -> None:
    with pytest.raises(ValueError, match="at least two"):
        await rehearse(PROMPT, PROVIDERS, workspace_factory(), n=1, model_factory=model_factory([SCRIPT]))
