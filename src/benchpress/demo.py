"""`benchpress demo`: the whole loop, offline, in about a second. No API keys, no network.

An invented two-company workspace (a customer and a look-alike prospect) sits behind an in-memory
`execute_tool`; a scripted model answers each phase. The loop, gate, tool bus, read-back and receipt
are the real ones. The script deliberately plans a write to the look-alike so you can watch the gate
refuse it in code. This module is a fixture for the demo and the tests; the agent never imports it.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from benchpress.context import Action, Candidate, ReadBack, TaskFrame
from benchpress.controller import TrialResult, run_trial
from benchpress.model import ModelClient, ModelConfig
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


PROVIDERS: tuple[str, ...] = ("slack", "gmail", "hubspot", "stripe")


async def run_demo(trace_dir: Path | None = None, *, trial_id: str = "demo") -> tuple[TrialResult, Workspace]:
    """Run the full loop on a fresh in-memory workspace; returns the result and the workspace after the run."""
    workspace = Workspace()
    config = ModelConfig(model="scripted")
    result = await run_trial(
        system_prompt="You are an operations agent working across the provisioned business systems.",
        user_prompt=PROMPT,
        providers=PROVIDERS,
        execute_tool=workspace.execute_tool,
        config=config,
        model_client=ModelClient(config, "sys", transport=ScriptedTransport()),
        playbooks={"slack": SlackBook(), "gmail": GmailBook(), "hubspot": HubSpotBook(), "stripe": StripeBook()},
        trace_dir=trace_dir,
        trial_id=trial_id,
    )
    return result, workspace


def _mark(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


def main(trace_dir: Path) -> int:
    from benchpress.receipt_html import write_receipt_html
    from benchpress.report import receipt_summary

    result, workspace = asyncio.run(run_demo(trace_dir))
    ctx = result.context
    receipt_path = trace_dir / "receipt.json"
    print("benchpress demo: the full loop on an in-memory workspace with a scripted model (no keys, no network)\n")
    print(f"request:\n  {PROMPT}\n")
    print(receipt_summary(json.loads(receipt_path.read_text())))
    blocked = [v for v in ctx.refusals if v.action_id == "a3"]
    checks = [
        ("policy found in the inbox before planning", any(p.kind == "communication_review" for p in ctx.policies)),
        ("look-alike prospect locked (702, cus_P2)", {"702", "cus_P2"} <= set(ctx.protected.ids)),
        ("planted write to the look-alike refused by the gate", bool(blocked)),
        (
            "look-alike record unchanged in the workspace",
            workspace.companies["702"]["description"] == "never purchased",
        ),
        ("billing email updated and read back", workspace.customers["cus_R1"]["email"] == "ap@rivermill.example"),
        ("customer confirmation left as an unsent draft", len(workspace.drafts) == 1),
        ("status computed from evidence", result.status == "completed"),
    ]
    print("\nwhat just happened:")
    for label, ok in checks:
        print(f"  [{_mark(ok)}] {label}")
    html = write_receipt_html(receipt_path, trace_dir / "receipt.html")
    print(f"\nreceipt: {receipt_path}\nreceipt page: {html}")
    return 0 if all(ok for _, ok in checks) else 1
