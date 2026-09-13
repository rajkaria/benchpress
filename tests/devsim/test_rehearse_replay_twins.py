"""Rehearse on devsim twins, then replay onto a fresh twin: real playbooks, real gateway, scripted model.

The seed is invented (example.com-style domains). The scripted model reads the twin's deterministic
ids from a probe stage so the resolve/plan answers point at records that exist.
"""

from __future__ import annotations

import json
from typing import Any, cast

from benchpress.model import ModelClient, ModelConfig
from benchpress.rehearse import rehearse, replay
from devsim.rehearse_stage import TwinStage, twin_stage_factory

PROVIDERS = ["stripe"]
SEED: dict[str, Any] = {
    "stripe": {
        "customers": [
            {"name": "Lindenfield Works", "email": "billing@lindenfield.example"},
            {"name": "Lindenfield Works Trial", "email": "hello@lindenfield-trial.example"},
        ]
    }
}
PROMPT = (
    "Lindenfield Works asked that its billing email move to ap@lindenfield.example. A similarly named trial "
    "account also exists and must not be touched. You are the billing operations specialist: make the change in "
    "the payments system and verify it. Do not create charges or alter unrelated customer records."
)


def _customer_ids() -> dict[str, str]:
    stage = TwinStage(PROVIDERS, SEED)
    customers = cast(dict[str, dict[str, Any]], stage.stores["stripe"].admin_state()["customers"])
    return {str(customer["email"]): cid for cid, customer in customers.items()}


def _script(target: str, other: str) -> dict[str, dict[str, Any]]:
    return {
        "emit_orient": {
            "role": "billing operations specialist",
            "subject_entities": ["Lindenfield Works"],
            "requested_change": "move the billing email to ap@lindenfield.example",
            "explicit_prohibitions": ["create charges", "alter unrelated customer records"],
            "distractor_hint": "similarly named trial account",
            "observed_identifiers": [],
        },
        "emit_policy_classify": {"policies": []},
        "emit_resolve": {
            "chosen": [
                {
                    "provider": "stripe",
                    "resource_type": "customer",
                    "resource_id": target,
                    "display": "Lindenfield Works",
                    "evidence": ["billing@lindenfield.example"],
                    "confidence": "high",
                }
            ],
            "ambiguous": False,
            "near_duplicate_ids": [other],
        },
        "emit_dod": {
            "summary": "billing email for Lindenfield Works is ap@lindenfield.example",
            "end_state": [
                {
                    "provider": "stripe",
                    "resource": f"customer:{target}",
                    "field": "email",
                    "expected": "ap@lindenfield.example",
                    "comparison": "email",
                }
            ],
            "facts": {"customer": "Lindenfield Works", "verified_contact": "ap@lindenfield.example"},
            "needs_customer_confirmation": False,
            "needs_owner_review": False,
            "forbidden": ["send_email", "create_charge"],
        },
        "emit_plan": {
            "actions": [
                {
                    "id": "a1",
                    "kind": "update",
                    "provider": "stripe",
                    "ref": f"customer:{target}",
                    "fields": {"email": "ap@lindenfield.example"},
                    "satisfies": ["end_state[0]"],
                }
            ]
        },
        "emit_repair": {"actions": []},
    }


class Transport:
    def __init__(self, script: dict[str, dict[str, Any]]) -> None:
        self.script = script

    async def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        name = str(cast(dict[str, Any], payload["tool_choice"])["function"]["name"])
        arguments = json.dumps(self.script.get(name, {}))
        call = {"id": "c1", "type": "function", "function": {"name": name, "arguments": arguments}}
        return {
            "model": "scripted",
            "choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [call]}, "finish_reason": "x"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }

    async def aclose(self) -> None:
        return None


async def test_rehearse_on_twins_then_replay_onto_a_fresh_twin_reaches_the_same_state() -> None:
    ids = _customer_ids()
    target, other = ids["billing@lindenfield.example"], ids["hello@lindenfield-trial.example"]
    script = _script(target, other)

    def model(_: int) -> ModelClient:
        return ModelClient(ModelConfig(model="scripted"), "sys", transport=Transport(script))

    rehearsal = await rehearse(PROMPT, PROVIDERS, twin_stage_factory(PROVIDERS, SEED), n=3, model_factory=model)
    assert rehearsal.converged, rehearsal.summary()
    assert [write.action.path for write in rehearsal.plan][:1] == [f"/v1/customers/{target}"]

    fresh = TwinStage(PROVIDERS, SEED)
    try:
        receipt = await replay(rehearsal, fresh.execute_tool, snapshot=fresh.snapshot)
        assert receipt.status == "completed", receipt.summary()
        assert receipt.state_matches_rehearsal is True
        customers = cast(dict[str, dict[str, Any]], fresh.stores["stripe"].admin_state()["customers"])
        assert customers[target]["email"] == "ap@lindenfield.example"
        assert customers[other]["email"] == "hello@lindenfield-trial.example"
    finally:
        await fresh.aclose()

    def tamper(stores: Any) -> None:
        stores["stripe"].customers[target]["name"] = "Lindenfield Works (renamed)"

    changed = TwinStage(PROVIDERS, SEED, mutate=tamper)
    try:
        stopped = await replay(rehearsal, changed.execute_tool, snapshot=changed.snapshot)
        assert stopped.status == "stopped" and stopped.stopped_at == 0, stopped.summary()
        assert any("name" in mismatch for mismatch in stopped.steps[0].mismatches), stopped.summary()
        assert stopped.state_matches_rehearsal is False
    finally:
        await changed.aclose()
