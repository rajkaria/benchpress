"""End-to-end replay of the whole loop on invented entities, with no network.

A scripted model answers each phase; fake playbooks talk to an in-memory workspace through
the real ToolBus, Gate and executor plumbing. This is the structural-determinism test: same
inputs, same plan, same evidence, same final JSON.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping, Sequence
from typing import Any, cast

import pytest

from benchpress.context import Action, Candidate, ReadBack, TaskFrame
from benchpress.controller import run_trial
from benchpress.model import ModelClient, ModelConfig
from benchpress.phases.common import Ablations
from benchpress.playbooks import PolicySource, ProviderRecord
from benchpress.tools import ToolBus

POLICY_TEXT = (
    "Billing-contact and tax-status changes require a customer confirmation reviewed by the account owner "
    "before sending."
)
PROMPT = (
    "Dana Reyes from customer success just posted in the #billing-desk Slack channel: Rivermill Studio asked for "
    "renewal notices to move to its accounts-payable address. The CRM contains a similarly named prospect that has "
    "never been a customer.\n\nYou're the billing operations specialist. Handle it in the systems we operate, verify "
    "the outcome, and keep the people relying on the result informed. Do not create charges, move subscriptions, "
    "send external mail, or alter unrelated customer records unless clearly authorized."
)


# --------------------------------------------------------------------------------------
# In-memory workspace + executor (the "twin")
# --------------------------------------------------------------------------------------


class Workspace:
    def __init__(self) -> None:
        self.companies: dict[str, dict[str, str]] = {
            "701": {
                "name": "Rivermill Studio",
                "domain": "rivermill.example",
                "description": "AP asked for renewal notices at ap@rivermill.example",
                "lifecyclestage": "customer",
            },
            "702": {
                "name": "Rivermill Studios Prospect",
                "domain": "rivermill-studios.example",
                "description": "never purchased",
                "lifecyclestage": "lead",
            },
        }
        self.customers: dict[str, dict[str, str]] = {
            "cus_R1": {"name": "Rivermill Studio", "email": "billing@rivermill.example"},
            "cus_P2": {"name": "Rivermill Studios Prospect", "email": "hello@rivermill-studios.example"},
        }
        self.channels: dict[str, str] = {"billing-desk": "C001"}
        self.messages: dict[str, list[dict[str, str]]] = {
            "C001": [
                {
                    "ts": "1.000001",
                    "user": "U1",
                    "text": "Rivermill Studio asked for renewal notices to move to its accounts-payable address.",
                }
            ]
        }
        self.mail: list[dict[str, Any]] = [
            {
                "id": "m1",
                "from": "records@corp.example",
                "subject": "Rivermill billing",
                "body": "Rivermill Studio customer uses billing@rivermill.example.",
            },
            {
                "id": "m2",
                "from": "operations-policy@corp.example",
                "subject": "Customer communication review policy",
                "body": POLICY_TEXT,
            },
        ]
        self.drafts: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []
        self.next_ts = 2

    async def execute_tool(self, tool_name: str, tool_input: dict[str, Any]) -> object:
        self.calls.append({"tool": tool_name, **tool_input})
        if tool_name != "provider_api":
            return {"ok": False, "error": "provider_docs unavailable"}
        provider, method, path = tool_input["provider"], tool_input["method"], tool_input["path"]
        body = cast(dict[str, Any], tool_input.get("body") or {})
        query = cast(dict[str, str], tool_input.get("query") or {})
        return {
            "ok": True,
            "status_code": 200,
            "body": self._route(provider, method, path, body, query),
            "trace": {"sequence": len(self.calls), "request_fingerprint": f"fp{len(self.calls)}"},
        }

    def _route(self, provider: str, method: str, path: str, body: dict[str, Any], query: dict[str, str]) -> object:
        if provider == "hubspot":
            if path.endswith("/search"):
                return {"results": [{"id": cid, "properties": props} for cid, props in self.companies.items()]}
            cid = path.rsplit("/", 1)[-1]
            if method == "PATCH":
                self.companies[cid].update(cast(dict[str, str], body.get("properties", {})))
            return {"id": cid, "properties": dict(self.companies[cid])}
        if provider == "stripe":
            if "search" in path or path == "/v1/customers":
                return {"data": [{"id": cid, **fields} for cid, fields in self.customers.items()]}
            cid = path.rsplit("/", 1)[-1]
            if method == "POST":
                self.customers[cid].update(cast(dict[str, str], body))
            return {"id": cid, **self.customers[cid]}
        if provider == "slack":
            if "conversations.list" in path:
                return {"ok": True, "channels": [{"id": cid, "name": name} for name, cid in self.channels.items()]}
            if "conversations.history" in path:
                return {"ok": True, "messages": list(reversed(self.messages[query["channel"]]))}
            if "chat.postMessage" in path:
                ts = f"{self.next_ts}.000000"
                self.next_ts += 1
                self.messages[body["channel"]].append({"ts": ts, "user": "BOT", "text": body["text"]})
                return {"ok": True, "ts": ts, "channel": body["channel"]}
            if "conversations.replies" in path:
                return {"ok": True, "messages": [m for m in self.messages[query["channel"]] if m["ts"] == query["ts"]]}
        if provider == "gmail":
            if path.endswith("/messages"):
                return {"messages": [{"id": m["id"]} for m in self.mail]}
            if "/messages/" in path:
                mid = path.rsplit("/", 1)[-1]
                return next(m for m in self.mail if m["id"] == mid)
            if path.endswith("/drafts") and method == "POST":
                raw = base64.urlsafe_b64decode(body["message"]["raw"] + "==").decode()
                draft = {"id": f"d{len(self.drafts) + 1}", "raw": raw, "labels": ["DRAFT"]}
                self.drafts.append(draft)
                return {"id": draft["id"], "message": {"id": "gm1", "labelIds": ["DRAFT"]}}
            if path.endswith("/drafts"):
                return {"drafts": [{"id": d["id"]} for d in self.drafts]}
            if "/drafts/" in path:
                did = path.rsplit("/", 1)[-1]
                draft = next(d for d in self.drafts if d["id"] == did)
                return {
                    "id": did,
                    "message": {"snippet": draft["raw"][:80], "raw": draft["raw"], "labelIds": ["DRAFT"]},
                }
        raise AssertionError(f"unrouted {provider} {method} {path}")


# --------------------------------------------------------------------------------------
# Fake playbooks (same interface as the real ones)
# --------------------------------------------------------------------------------------


class FakeBook:
    provider = ""
    role = ""
    identity_fields: tuple[str, ...] = ("name",)

    async def policy_sources(self, bus: ToolBus, frame: TaskFrame) -> list[PolicySource]:
        return []

    async def find_candidates(self, bus: ToolBus, entity: str, hints: Sequence[str]) -> list[Candidate]:
        return []

    async def read_record(self, bus: ToolBus, ref: str) -> ProviderRecord | None:
        return None

    async def read_field(self, bus: ToolBus, ref: str, field: str) -> str | None:
        record = await self.read_record(bus, ref)
        return record.fields.get(field) if record else None

    def update_action(
        self,
        action_id: str,
        ref: str,
        fields: Mapping[str, str],
        satisfies: Sequence[str],
        *,
        target_refs: Sequence[str] = (),
        rationale: str = "",
    ) -> Action | None:
        return None

    def message_action(
        self,
        action_id: str,
        channel_id: str,
        text: str,
        satisfies: Sequence[str],
        *,
        thread_ts: str | None = None,
        target_refs: Sequence[str] = (),
    ) -> Action | None:
        return None

    def draft_action(
        self,
        action_id: str,
        to: str,
        subject: str,
        body: str,
        satisfies: Sequence[str],
        *,
        target_refs: Sequence[str] = (),
    ) -> Action | None:
        return None

    async def resolve_channel(self, bus: ToolBus, name: str) -> str | None:
        return None

    async def channel_history(self, bus: ToolBus, channel_id: str, limit: int = 50) -> list[dict[str, object]]:
        return []

    async def list_drafts(self, bus: ToolBus) -> list[dict[str, object]]:
        return []

    async def list_channel_messages_since(
        self, bus: ToolBus, channel_id: str, oldest_ts: str
    ) -> list[dict[str, object]]:
        return []


class HubSpotBook(FakeBook):
    provider = "hubspot"
    role = "hubspot_crm"

    async def find_candidates(self, bus: ToolBus, entity: str, hints: Sequence[str]) -> list[Candidate]:
        result = await bus.read("hubspot", "/crm/v3/objects/companies/search", method="POST", body={"query": entity})
        return [
            Candidate(
                provider="hubspot",
                resource_type="company",
                resource_id=str(item["id"]),
                display=item["properties"]["name"],
                name=item["properties"]["name"],
                domain=item["properties"]["domain"],
                lifecycle=item["properties"]["lifecyclestage"],
                notes=item["properties"]["description"],
            )
            for item in cast(list[dict[str, Any]], result.json()["results"])
        ]

    async def read_record(self, bus: ToolBus, ref: str) -> ProviderRecord | None:
        cid = ref.split(":", 1)[1]
        result = await bus.read("hubspot", f"/crm/v3/objects/companies/{cid}")
        return ProviderRecord(
            provider="hubspot", resource_type="company", resource_id=cid, fields=dict(result.json()["properties"])
        )

    def update_action(
        self,
        action_id: str,
        ref: str,
        fields: Mapping[str, str],
        satisfies: Sequence[str],
        *,
        target_refs: Sequence[str] = (),
        rationale: str = "",
    ) -> Action | None:
        cid = ref.split(":", 1)[1]
        return Action(
            id=action_id,
            kind="update",
            provider="hubspot",
            method="PATCH",
            path=f"/crm/v3/objects/companies/{cid}",
            body={"properties": dict(fields)},
            fields=tuple(fields),
            satisfies=tuple(satisfies),
            target_refs=tuple(target_refs),
            readback=ReadBack(path=f"/crm/v3/objects/companies/{cid}", field_path="properties"),
            rationale=rationale,
        )


class StripeBook(FakeBook):
    provider = "stripe"
    role = "payments"

    async def find_candidates(self, bus: ToolBus, entity: str, hints: Sequence[str]) -> list[Candidate]:
        result = await bus.read("stripe", "/v1/customers/search", query={"query": f"name~'{entity}'"})
        return [
            Candidate(
                provider="stripe",
                resource_type="customer",
                resource_id=item["id"],
                display=item["name"],
                name=item["name"],
                email=item["email"],
            )
            for item in cast(list[dict[str, Any]], result.json()["data"])
        ]

    async def read_record(self, bus: ToolBus, ref: str) -> ProviderRecord | None:
        cid = ref.split(":", 1)[1]
        result = await bus.read("stripe", f"/v1/customers/{cid}")
        payload = result.json()
        return ProviderRecord(
            provider="stripe",
            resource_type="customer",
            resource_id=cid,
            fields={"name": payload["name"], "email": payload["email"]},
        )

    def update_action(
        self,
        action_id: str,
        ref: str,
        fields: Mapping[str, str],
        satisfies: Sequence[str],
        *,
        target_refs: Sequence[str] = (),
        rationale: str = "",
    ) -> Action | None:
        cid = ref.split(":", 1)[1]
        return Action(
            id=action_id,
            kind="update",
            provider="stripe",
            method="POST",
            path=f"/v1/customers/{cid}",
            body=dict(fields),
            body_encoding="form",
            fields=tuple(fields),
            satisfies=tuple(satisfies),
            target_refs=tuple(target_refs),
            headers={"Idempotency-Key": f"bp-{action_id}"},
            readback=ReadBack(path=f"/v1/customers/{cid}"),
            rationale=rationale,
        )


class SlackBook(FakeBook):
    provider = "slack"
    role = "team_chat"

    async def resolve_channel(self, bus: ToolBus, name: str) -> str | None:
        result = await bus.read("slack", "/api/conversations.list")
        return next(
            (str(c["id"]) for c in cast(list[dict[str, Any]], result.json()["channels"]) if c["name"] == name), None
        )

    async def channel_history(self, bus: ToolBus, channel_id: str, limit: int = 50) -> list[dict[str, object]]:
        result = await bus.read(
            "slack", "/api/conversations.history", query={"channel": channel_id, "limit": str(limit)}
        )
        return cast(list[dict[str, object]], result.json()["messages"])

    async def policy_sources(self, bus: ToolBus, frame: TaskFrame) -> list[PolicySource]:
        channel_id = await self.resolve_channel(bus, frame.originating_channel or "")
        if not channel_id:
            return []
        return [
            PolicySource(provider="slack", resource_ref=f"channel:{channel_id}/message:{m['ts']}", text=str(m["text"]))
            for m in await self.channel_history(bus, channel_id)
        ]

    def message_action(
        self,
        action_id: str,
        channel_id: str,
        text: str,
        satisfies: Sequence[str],
        *,
        thread_ts: str | None = None,
        target_refs: Sequence[str] = (),
    ) -> Action | None:
        return Action(
            id=action_id,
            kind="message",
            provider="slack",
            method="POST",
            path="/api/chat.postMessage",
            body={"channel": channel_id, "text": text},
            fields=("channel", "text"),
            satisfies=tuple(satisfies),
            target_refs=tuple(target_refs),
            readback=ReadBack(
                path="/api/conversations.replies",
                query={"channel": channel_id, "ts": "{created_ts}"},
                field_path="messages.0.text",
            ),
        )

    async def list_channel_messages_since(
        self, bus: ToolBus, channel_id: str, oldest_ts: str
    ) -> list[dict[str, object]]:
        return [m for m in await self.channel_history(bus, channel_id) if str(m["ts"]) > oldest_ts]


class GmailBook(FakeBook):
    provider = "gmail"
    role = "email"

    async def policy_sources(self, bus: ToolBus, frame: TaskFrame) -> list[PolicySource]:
        listing = await bus.read("gmail", "/gmail/v1/users/me/messages", query={"maxResults": "50"})
        sources: list[PolicySource] = []
        for item in cast(list[dict[str, Any]], listing.json()["messages"]):
            message = (
                await bus.read("gmail", f"/gmail/v1/users/me/messages/{item['id']}", query={"format": "full"})
            ).json()
            sources.append(
                PolicySource(
                    provider="gmail",
                    resource_ref=f"message:{item['id']}",
                    title=str(message["subject"]),
                    author=str(message["from"]),
                    text=f"{message['subject']}\n{message['body']}",
                )
            )
        return sources

    def draft_action(
        self,
        action_id: str,
        to: str,
        subject: str,
        body: str,
        satisfies: Sequence[str],
        *,
        target_refs: Sequence[str] = (),
    ) -> Action | None:
        raw = base64.urlsafe_b64encode(f"To: {to}\r\nSubject: {subject}\r\n\r\n{body}".encode()).decode().rstrip("=")
        return Action(
            id=action_id,
            kind="draft",
            provider="gmail",
            method="POST",
            path="/gmail/v1/users/me/drafts",
            body={"message": {"raw": raw}},
            fields=("message", "raw"),
            satisfies=tuple(satisfies),
            target_refs=tuple(target_refs),
            readback=ReadBack(
                path="/gmail/v1/users/me/drafts/{created_id}", query={"format": "full"}, field_path="message.snippet"
            ),
        )

    async def list_drafts(self, bus: ToolBus) -> list[dict[str, object]]:
        listing = await bus.read("gmail", "/gmail/v1/users/me/drafts")
        out: list[dict[str, object]] = []
        for item in cast(list[dict[str, Any]], listing.json()["drafts"]):
            detail = (
                await bus.read("gmail", f"/gmail/v1/users/me/drafts/{item['id']}", query={"format": "full"})
            ).json()
            out.append({"id": item["id"], "body": detail["message"]["raw"], "labels": detail["message"]["labelIds"]})
        return out


# --------------------------------------------------------------------------------------
# Scripted model: answers by the phase tool it is forced to call
# --------------------------------------------------------------------------------------

SCRIPT: dict[str, dict[str, Any]] = {
    "emit_orient": {
        "reporter": "Dana Reyes",
        "originating_channel": "#billing-desk Slack channel",
        "role": "billing operations specialist",
        "subject_entities": ["Rivermill Studio"],
        "requested_change": "move renewal notices to the accounts-payable address",
        "explicit_prohibitions": ["create charges", "send external mail"],
        "distractor_hint": "similarly named prospect",
        "observed_identifiers": [],
    },
    "emit_policy_classify": {"policies": []},  # the deterministic rule must catch the policy without the model
    "emit_resolve": {
        "chosen": [
            {
                "provider": "hubspot",
                "resource_type": "company",
                "resource_id": "701",
                "display": "Rivermill Studio",
                "evidence": ["rivermill.example", "customer"],
                "confidence": "high",
            },
            {
                "provider": "stripe",
                "resource_type": "customer",
                "resource_id": "cus_R1",
                "display": "Rivermill Studio",
                "evidence": ["billing@rivermill.example"],
                "confidence": "high",
            },
        ],
        "ambiguous": False,
        "near_duplicate_ids": ["702", "cus_P2"],
    },
    "emit_dod": {
        "summary": "renewal notices go to ap@rivermill.example for Rivermill Studio",
        "end_state": [
            {
                "provider": "stripe",
                "resource": "customer:cus_R1",
                "field": "email",
                "expected": "ap@rivermill.example",
                "comparison": "email",
            },
            {
                "provider": "hubspot",
                "resource": "company:701",
                "field": "description",
                "expected": "ap@rivermill.example",
                "comparison": "contains",
            },
        ],
        "facts": {
            "customer": "Rivermill Studio",
            "former_contact": "billing@rivermill.example",
            "verified_contact": "ap@rivermill.example",
        },
        "customer_contact_email": "ap@rivermill.example",
        "account_owner": "Dana Reyes",
        "needs_customer_confirmation": True,
        "needs_owner_review": True,
        "forbidden": ["send_email", "create_charge"],
    },
    "emit_plan": {
        "actions": [
            {
                "id": "a1",
                "kind": "update",
                "provider": "stripe",
                "ref": "customer:cus_R1",
                "fields": {"email": "ap@rivermill.example"},
                "satisfies": ["end_state[0]"],
            },
            {
                "id": "a2",
                "kind": "update",
                "provider": "hubspot",
                "ref": "company:701",
                "fields": {
                    "description": "Renewal notices go to ap@rivermill.example (formerly billing@rivermill.example)"
                },
                "satisfies": ["end_state[1]"],
            },
            {
                "id": "a3",
                "kind": "update",
                "provider": "hubspot",
                "ref": "company:702",
                "fields": {"description": "touched the prospect"},
                "satisfies": ["end_state[1]"],
            },
        ]
    },
    "emit_repair": {"actions": []},
}


class ScriptedTransport:
    def __init__(self) -> None:
        self.phases: list[str] = []

    async def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        choice = cast(dict[str, Any], payload["tool_choice"])
        name = str(choice["function"]["name"])
        self.phases.append(name)
        return {
            "model": "scripted",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "c1",
                                "type": "function",
                                "function": {"name": name, "arguments": json.dumps(SCRIPT[name])},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 50, "completion_tokens": 10},
        }

    async def aclose(self) -> None:
        return None


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
