"""Provider playbooks: request shapes, response parsing, write constructors, bounded budgets.

Every provider call goes through a fake `execute_tool` that answers with harness-style envelopes
(`{"ok", "status_code", "body", …}`, see `tools._interpret`) and records the exact request the
playbook made. The entities are invented (the conftest "Rivermill" style); none of this refers
to a benchmark task.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from benchpress import playbooks
from benchpress.context import Action, Context, DefinitionOfDone, ReadBack, TaskFrame
from benchpress.gate import Gate
from benchpress.playbooks import (
    CREATED_ID_PLACEHOLDER,
    CREATED_TS_PLACEHOLDER,
    BasePlaybook,
    PolicySource,
    available_providers,
    entity_terms,
    extract_field,
    fill_placeholders,
    flat_fields,
    for_provider,
    has_placeholders,
    read_json,
)
from benchpress.playbooks import gmail as gmail_module
from benchpress.playbooks import stripe as stripe_module
from benchpress.playbooks.gmail import (
    DRAFTS_PATH,
    MESSAGES_PATH,
    GmailPlaybook,
    decode_message,
    encode_rfc2822,
    parse_rfc2822,
)
from benchpress.playbooks.hubspot import HubSpotPlaybook, company_filter_groups, contact_filter_groups
from benchpress.playbooks.slack import SlackPlaybook, select_policy_channels, user_directory
from benchpress.playbooks.stripe import StripePlaybook, idempotency_key, quote, search_clauses
from benchpress.tools import ToolBus

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "playbooks"


def fixture(name: str) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8")))


# --------------------------------------------------------------------------------------
# Fake gateway
# --------------------------------------------------------------------------------------


@dataclass
class Route:
    method: str
    path: str
    body: object
    status: int = 200
    query: dict[str, str] | None = None
    where: Callable[[dict[str, Any]], bool] | None = None

    def matches(self, payload: dict[str, Any]) -> bool:
        if payload.get("method") != self.method or payload.get("path") != self.path:
            return False
        if self.query is not None:
            actual = cast(dict[str, Any], payload.get("query") or {})
            if any(actual.get(key) != value for key, value in self.query.items()):
                return False
        return self.where is None or self.where(payload)


class FakeGateway:
    """Answers with harness-style envelopes and records every request payload."""

    def __init__(self, routes: Sequence[Route]) -> None:
        self.routes = list(routes)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, tool_name: str, payload: dict[str, Any]) -> object:
        assert tool_name == "provider_api"
        self.calls.append(payload)
        for route in self.routes:
            if route.matches(payload):
                return self.envelope(payload, route.status, route.body)
        return self.envelope(payload, 404, {"error": "not_found"})

    def envelope(self, payload: dict[str, Any], status: int, body: object) -> dict[str, Any]:
        ok = 200 <= status < 300
        return {
            "ok": ok,
            "requested_provider": payload.get("provider"),
            "provider": payload.get("provider"),
            "method": payload.get("method"),
            "path": payload.get("path"),
            "status_code": status,
            "headers": {},
            "body": body,
            "truncated": False,
            "error": None if ok else f"HTTP {status}",
            "trace": {"sequence": len(self.calls)},
        }

    def to(self, path: str, method: str | None = None) -> list[dict[str, Any]]:
        return [
            call for call in self.calls if call.get("path") == path and (method is None or call.get("method") == method)
        ]


def make_bus(*routes: Route, phase: str = "P2") -> tuple[ToolBus, FakeGateway]:
    context = Context(trial_id="t-playbooks", providers=("slack", "gmail", "hubspot", "stripe"))
    gateway = FakeGateway(routes)
    bus = ToolBus(context=context, execute=gateway, gate=Gate(context=context, allow_unplanned=True))
    bus.enter(phase)
    return bus, gateway


def no_cursor(payload: dict[str, Any]) -> bool:
    return "cursor" not in cast(dict[str, Any], payload.get("query") or {})


def no_starting_after(payload: dict[str, Any]) -> bool:
    return "starting_after" not in cast(dict[str, Any], payload.get("query") or {})


def slack_routes() -> list[Route]:
    return [
        Route("GET", "/api/conversations.list", fixture("slack_conversations_list"), where=no_cursor),
        Route(
            "GET",
            "/api/conversations.list",
            fixture("slack_conversations_list_page2"),
            query={"cursor": "dGVhbTpDMDFCSUxMMDAwMQ=="},
        ),
        Route(
            "GET",
            "/api/conversations.history",
            fixture("slack_conversations_history_billing"),
            query={"channel": "C01BILL0001"},
        ),
        Route("GET", "/api/conversations.history", fixture("slack_conversations_history_policy")),
        Route("GET", "/api/users.list", fixture("slack_users_list")),
        Route("GET", "/api/conversations.replies", fixture("slack_conversations_replies")),
    ]


slack = SlackPlaybook()
gmail = GmailPlaybook()
hubspot = HubSpotPlaybook()
stripe = StripePlaybook()


# --------------------------------------------------------------------------------------
# Registry and shared helpers
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("token", "provider"),
    [
        ("slack", "slack"),
        ("team_chat", "slack"),
        ("gmail", "gmail"),
        ("email", "gmail"),
        ("hubspot", "hubspot"),
        ("hubspot_crm", "hubspot"),
        ("stripe", "stripe"),
        ("payments", "stripe"),
        ("github", "github"),
        ("code_host", "github"),
        (" Slack ", "slack"),
    ],
)
def test_for_provider_accepts_names_and_roles(token: str, provider: str) -> None:
    playbook = for_provider(token)
    assert playbook is not None
    assert playbook.provider == provider


def test_for_provider_unknown_is_none_and_available_providers_lists_names() -> None:
    assert for_provider("salesforce") is None
    assert for_provider("") is None
    assert available_providers() == ("github", "gmail", "hubspot", "slack", "stripe")
    assert set(playbooks.PLAYBOOKS) == {
        "slack",
        "team_chat",
        "gmail",
        "email",
        "hubspot",
        "hubspot_crm",
        "stripe",
        "payments",
        "github",
        "code_host",
    }


async def test_base_playbook_defaults_are_inert() -> None:
    bus, gateway = make_bus()
    base = BasePlaybook()
    frame = TaskFrame(originating_channel="billing-desk")
    assert await base.policy_sources(bus, frame) == []
    assert await base.find_candidates(bus, "Harlow Bakery", ()) == []
    assert await base.read_record(bus, "company:1") is None
    assert await base.read_field(bus, "company:1", "name") is None
    assert base.update_action("a1", "company:1", {"name": "x"}, ("end_state[0]",)) is None
    assert base.message_action("a1", "C1", "hi", ("deliverable[0]",)) is None
    assert base.draft_action("a1", "a@b.example", "s", "b", ("deliverable[1]",)) is None
    assert await base.resolve_channel(bus, "billing-desk") is None
    assert await base.channel_history(bus, "C1") == []
    assert await base.list_drafts(bus) == []
    assert await base.list_channel_messages_since(bus, "C1", "1.0") == []
    assert gateway.calls == []


@pytest.mark.parametrize(
    ("field_path", "expected"),
    [
        ("messages.0.text", "hello"),
        ("messages.0.count", "3"),
        ("messages.0.ok", "true"),
        ("messages.0.meta", '{"a": 1}'),
        ("messages.-1.text", "hello"),
        ("messages.1.text", None),
        ("messages.0.missing", None),
        ("messages.0.gone", None),
        ("messages.x.text", None),
        ("nope", None),
        ("properties.description", "notes"),
        ("", None),
    ],
)
def test_extract_field(field_path: str, expected: str | None) -> None:
    payload = {
        "messages": [{"text": "hello", "count": 3, "ok": True, "meta": {"a": 1}, "gone": None}],
        "properties": {"description": "notes"},
    }
    if field_path == "":
        assert extract_field(payload, "") == json.dumps(payload, sort_keys=True, ensure_ascii=False)
    else:
        assert extract_field(payload, field_path) == expected


def test_extract_field_on_scalars_and_none() -> None:
    assert extract_field(None, "a") is None
    assert extract_field("plain", "") == "plain"
    assert extract_field("plain", "a") is None
    assert extract_field({"a": [1, 2]}, "a") == "[1, 2]"


def test_fill_placeholders_substitutes_from_response() -> None:
    slack_readback = ReadBack(
        path="/api/conversations.replies",
        query={"channel": "C01BILL0001", "ts": CREATED_TS_PLACEHOLDER, "limit": "1"},
        field_path="messages.0.text",
    )
    assert has_placeholders(slack_readback)
    filled = fill_placeholders(slack_readback, fixture("slack_chat_postmessage"))
    assert filled.query == {"channel": "C01BILL0001", "ts": "1757691000.000300", "limit": "1"}
    assert not has_placeholders(filled)

    gmail_readback = ReadBack(path=f"{DRAFTS_PATH}/{CREATED_ID_PLACEHOLDER}", query={"format": "full"})
    filled = fill_placeholders(gmail_readback, {"id": "r-7781", "message": {"id": "msg_d1e2f3a4b5c6"}})
    assert filled.path == f"{DRAFTS_PATH}/r-7781"
    # A response without the value leaves the placeholder in place rather than inventing one.
    assert has_placeholders(fill_placeholders(gmail_readback, {"ok": True}))


def test_entity_terms_classifies_hints() -> None:
    terms = entity_terms(
        "Harlow Bakery",
        ["ap@harlowbakery.example", "harlow-bakeries.example", "cus_HB001a2b3c4d5", "9001", "Harlow Bakery Ltd", ""],
    )
    assert terms.names == ("Harlow Bakery", "Harlow Bakery Ltd")
    assert terms.tokens == ("bakery", "harlow", "ltd")
    assert terms.domains == ("harlowbakery.example", "harlow-bakeries.example")
    assert terms.emails == ("ap@harlowbakery.example",)
    assert terms.ids == ("cus_HB001a2b3c4d5", "9001")
    assert not terms.empty
    assert entity_terms("", []).empty
    assert entity_terms("ap@harlowbakery.example", []).emails == ("ap@harlowbakery.example",)


def test_flat_fields_flattens_nested_records() -> None:
    flat = flat_fields({"id": "cus_1", "metadata": {"lifecycle": "customer"}, "tags": ["a", "b"], "phone": None})
    assert flat == {"id": "cus_1", "metadata.lifecycle": "customer", "tags": "a, b"}


async def test_read_json_handles_transport_failures_without_raising() -> None:
    async def explode(tool_name: str, payload: dict[str, Any]) -> object:
        raise RuntimeError("connection reset")

    context = Context(trial_id="t-playbooks")
    bus = ToolBus(context=context, execute=explode, gate=Gate(context=context, allow_unplanned=True))
    bus.enter("P2")
    assert await read_json(bus, "slack", "/api/conversations.list") is None
    assert context.ledger[-1].ok is False


# --------------------------------------------------------------------------------------
# Slack
# --------------------------------------------------------------------------------------


async def test_slack_resolve_channel_paginates_and_normalizes_names() -> None:
    bus, gateway = make_bus(*slack_routes())
    assert await slack.resolve_channel(bus, "#Eng-Guidelines") == "C01GUID0006"
    lists = gateway.to("/api/conversations.list", "GET")
    assert len(lists) == 2
    assert lists[0]["query"] == {"types": "public_channel,private_channel", "limit": "200"}
    assert lists[1]["query"]["cursor"] == "dGVhbTpDMDFCSUxMMDAwMQ=="
    assert lists[0]["provider"] == "slack"


async def test_slack_resolve_channel_first_page_hit_stops_paginating() -> None:
    bus, gateway = make_bus(*slack_routes())
    assert await slack.resolve_channel(bus, "billing-desk") == "C01BILL0001"
    # The page-1 hit is only found after both pages are listed once (pagination completes first).
    assert len(gateway.to("/api/conversations.list")) == 2
    assert await slack.resolve_channel(bus, "C01POLC0002") == "C01POLC0002"
    assert await slack.resolve_channel(bus, "no-such-channel") is None
    assert await slack.resolve_channel(bus, "   ") is None


async def test_slack_ok_false_is_a_failure() -> None:
    bus, gateway = make_bus(
        Route("GET", "/api/conversations.list", fixture("slack_ok_false")),
        Route("GET", "/api/conversations.history", fixture("slack_ok_false")),
    )
    assert await slack.resolve_channel(bus, "billing-desk") is None
    assert await slack.channel_history(bus, "C01BILL0001") == []
    assert await slack.list_channel_messages_since(bus, "C01BILL0001", "1757690000.000100") == []
    assert all(call["method"] == "GET" for call in gateway.calls)


async def test_slack_channel_history_request_shape() -> None:
    bus, gateway = make_bus(*slack_routes())
    messages = await slack.channel_history(bus, "C01BILL0001", limit=50)
    assert [message["ts"] for message in messages] == ["1757690000.000100", "1757689500.000150", "1757689000.000200"]
    (call,) = gateway.to("/api/conversations.history")
    assert call["method"] == "GET"
    assert call["query"] == {"channel": "C01BILL0001", "limit": "50"}
    assert "body" not in call


async def test_slack_messages_since_uses_oldest() -> None:
    bus, gateway = make_bus(*slack_routes())
    await slack.list_channel_messages_since(bus, "C01BILL0001", "1757689000.000200")
    (call,) = gateway.to("/api/conversations.history")
    assert call["query"] == {
        "channel": "C01BILL0001",
        "oldest": "1757689000.000200",
        "inclusive": "false",
        "limit": "100",
    }


async def test_slack_policy_sources_select_channels_and_resolve_authors() -> None:
    bus, gateway = make_bus(*slack_routes(), phase="P1")
    frame = TaskFrame(originating_channel="#billing-desk", subject_entities=("Harlow Bakery",))
    sources = await slack.policy_sources(bus, frame)

    histories = gateway.to("/api/conversations.history")
    requested = [call["query"]["channel"] for call in histories]
    # Originating channel first, then policy-named live channels by name, four in total.
    assert requested == ["C01BILL0001", "C01ANNC0004", "C01GUID0006", "C01POLC0002"]
    assert "C01UPDT0005" not in requested  # archived
    assert "C01RAND0003" not in requested  # not policy-named
    assert "C01PUPD0007" not in requested  # over the cap
    assert all(call["query"]["limit"] == "50" for call in histories)
    assert len(gateway.to("/api/users.list")) == 1
    assert len(gateway.calls) == 7

    billing = [source for source in sources if source.resource_ref.startswith("channel:C01BILL0001/")]
    assert [source.author for source in billing] == ["priya", "Tomas Reyes"]  # join message skipped
    assert billing[0].resource_ref == "channel:C01BILL0001/message:1757690000.000100"
    assert billing[0].title == "#billing-desk"
    assert billing[0].path == "/api/conversations.history?channel=C01BILL0001"
    assert "ap@harlowbakery.example" in billing[0].text
    assert all(source.provider == "slack" for source in sources)
    assert len(sources) == 2 + 3


async def test_slack_policy_sources_without_channels_is_empty() -> None:
    bus, _ = make_bus(Route("GET", "/api/conversations.list", {"ok": True, "channels": []}))
    assert await slack.policy_sources(bus, TaskFrame(originating_channel="billing-desk")) == []


def test_select_policy_channels_caps_and_orders() -> None:
    channels = fixture("slack_conversations_list")["channels"] + fixture("slack_conversations_list_page2")["channels"]
    selected = select_policy_channels(channels, "billing-desk", cap=3)
    assert [channel["name"] for channel in selected] == ["billing-desk", "announcements", "eng-guidelines"]
    selected = select_policy_channels(channels, None)
    assert [channel["name"] for channel in selected] == [
        "announcements",
        "eng-guidelines",
        "ops-policy",
        "product-updates",
    ]


def test_user_directory_prefers_display_then_real_name() -> None:
    directory = user_directory(fixture("slack_users_list")["members"])
    assert directory == {"U01PRIYA001": "priya", "U01TOMAS002": "Tomas Reyes", "U01BOT00099": "Benchpress"}


def test_slack_message_action_shape() -> None:
    action = slack.message_action(
        "a7",
        "C01BILL0001",
        "Verified: Harlow Bakery renewal notices now go to ap@harlowbakery.example.",
        ("deliverable[originating_channel_update]",),
        target_refs=("company:9001",),
    )
    assert action is not None
    assert (action.kind, action.provider, action.method, action.path) == (
        "message",
        "slack",
        "POST",
        "/api/chat.postMessage",
    )
    assert action.body == {
        "channel": "C01BILL0001",
        "text": "Verified: Harlow Bakery renewal notices now go to ap@harlowbakery.example.",
    }
    assert action.body_encoding == "json"
    assert action.fields == ("channel", "text")
    assert action.satisfies == ("deliverable[originating_channel_update]",)
    assert action.target_refs == ("company:9001", "channel:C01BILL0001")
    assert action.readback is not None
    assert action.readback.method == "GET"
    assert action.readback.path == "/api/conversations.replies"
    assert action.readback.query == {"channel": "C01BILL0001", "ts": CREATED_TS_PLACEHOLDER, "limit": "1"}
    assert action.readback.field_path == "messages.0.text"
    assert action.rationale

    filled = fill_placeholders(action.readback, fixture("slack_chat_postmessage"))
    posted = cast(dict[str, str], action.body)["text"]
    assert extract_field(fixture("slack_conversations_replies"), filled.field_path or "") == posted


def test_slack_message_action_thread_and_invalid_inputs() -> None:
    threaded = slack.message_action("a8", "C01BILL0001", "Review request", ("d[1]",), thread_ts="1757690000.000100")
    assert threaded is not None
    assert threaded.body == {"channel": "C01BILL0001", "text": "Review request", "thread_ts": "1757690000.000100"}
    assert threaded.fields == ("channel", "text", "thread_ts")
    assert slack.message_action("a9", "", "text", ("d[1]",)) is None
    assert slack.message_action("a9", "C01BILL0001", "   ", ("d[1]",)) is None
    assert slack.message_action("", "C01BILL0001", "text", ("d[1]",)) is None
    assert slack.message_action("a9", "C01BILL0001", "x" * 40_001, ("d[1]",)) is None


async def test_slack_read_record_message_and_channel() -> None:
    bus, gateway = make_bus(*slack_routes())
    record = await slack.read_record(bus, "channel:C01BILL0001/message:1757691000.000300")
    assert record is not None
    assert record.resource_type == "message"
    assert record.fields["text"].startswith("Verified: Harlow Bakery")
    (call,) = gateway.to("/api/conversations.replies")
    assert call["query"] == {"channel": "C01BILL0001", "ts": "1757691000.000300", "limit": "1"}
    assert await slack.read_field(bus, "channel:C01BILL0001/message:1757691000.000300", "user") == "U01BOT00099"

    channel = await slack.read_record(bus, "channel:C01POLC0002")
    assert channel is not None
    assert channel.fields["name"] == "ops-policy"
    assert channel.fields["purpose"] == "Operating rules for the ops team"
    assert await slack.read_record(bus, "company:9001") is None
    assert await slack.read_record(bus, "channel:C01BILL0001/message:999.1") is None


# --------------------------------------------------------------------------------------
# Gmail
# --------------------------------------------------------------------------------------


def test_gmail_decode_multipart_prefers_plain_and_accepts_unpadded_base64url() -> None:
    decoded = decode_message(fixture("gmail_message_multipart"))
    assert decoded["id"] == "msg_a1f3e9c2b7d4"
    assert decoded["thread_id"] == "thr_a1f3e9c2b7d4"
    assert decoded["labels"] == ["INBOX", "IMPORTANT"]
    assert decoded["from"] == "Ops Policy <ops-policy@brightline.example>"
    assert decoded["subject"] == "Policy: customer confirmations require owner review"
    body = cast(str, decoded["body"])
    assert body.startswith("Reminder from operations policy:")
    assert "reviewed by the account owner before sending" in body
    assert "<p>" not in body and "<b>" not in body


def test_gmail_decode_single_part_plain_and_raw_fallback() -> None:
    plain = decode_message(fixture("gmail_message_plain"))
    assert plain["from"] == "Dana Okafor <ap@harlowbakery.example>"
    assert cast(str, plain["body"]).endswith("Accounts Payable, Harlow Bakery")

    raw_only = decode_message(fixture("gmail_message_raw_only"))
    assert raw_only["subject"] == "Renewal notices for Harlow Bakery"
    assert raw_only["from"] == "Dana Okafor <ap@harlowbakery.example>"
    assert cast(str, raw_only["body"]).startswith("Please route renewal notices to ap@harlowbakery.example")


def test_gmail_decode_html_only_strips_tags_and_bare_payload() -> None:
    html_data = gmail_module.b64url_encode(b"<p>Hold all <b>renewal</b> notices &amp; wait.</p>")
    decoded = decode_message({"id": "m9", "payload": {"mimeType": "text/html", "body": {"data": html_data}}})
    assert decoded["body"] == "Hold all renewal notices & wait."
    bare = decode_message({"mimeType": "text/plain", "body": {"data": gmail_module.b64url_encode(b"plain text")}})
    assert bare["body"] == "plain text"
    assert decode_message({"id": "m10", "snippet": "only a snippet"})["body"] == "only a snippet"
    assert gmail_module.b64url_decode("!!! not base64 !!!") == b""


def test_encode_rfc2822_round_trip() -> None:
    body = "Hello Dana,\n\nRenewal notices for Harlow Bakery now go to ap@harlowbakery.example.\n\nThanks"
    raw = encode_rfc2822("ap@harlowbakery.example", "Confirmation: renewal notices for Harlow Bakery", body)
    wire = gmail_module.b64url_decode(raw)
    assert wire.startswith(
        b"To: ap@harlowbakery.example\r\nSubject: Confirmation: renewal notices for Harlow Bakery\r\n"
    )
    assert b"MIME-Version: 1.0\r\n" in wire
    assert b'Content-Type: text/plain; charset="utf-8"\r\n' in wire
    assert b"\r\n\r\nHello Dana,\r\n\r\nRenewal notices" in wire

    parsed = parse_rfc2822(raw)
    assert parsed["To"] == "ap@harlowbakery.example"
    assert parsed["Subject"] == "Confirmation: renewal notices for Harlow Bakery"
    assert parsed.get_content_type() == "text/plain"
    assert parsed.get_content().replace("\r\n", "\n").rstrip("\n") == body

    decoded = decode_message({"id": "draft-msg", "raw": raw})
    assert decoded["to"] == "ap@harlowbakery.example"
    assert decoded["subject"] == "Confirmation: renewal notices for Harlow Bakery"
    assert cast(str, decoded["body"]).replace("\r\n", "\n").rstrip("\n") == body


def test_encode_rfc2822_non_ascii_round_trip() -> None:
    raw = encode_rfc2822("Dana Okafor <ap@harlowbakery.example>", "Confirmación — Harlow Bakery", "Café renewal ✔")
    parsed = parse_rfc2822(raw)
    assert parsed["Subject"] == "Confirmación — Harlow Bakery"
    assert parsed["To"] == "Dana Okafor <ap@harlowbakery.example>"
    assert parsed.get_content().rstrip("\n") == "Café renewal ✔"
    assert b"Content-Transfer-Encoding: 8bit" in gmail_module.b64url_decode(raw)


def test_encode_rfc2822_neutralizes_header_injection() -> None:
    raw = encode_rfc2822(
        "ap@harlowbakery.example\r\nBcc: collector@elsewhere.example",
        "Subject line\nX-Injected: yes",
        "body",
    )
    parsed = parse_rfc2822(raw)
    assert parsed["Bcc"] is None
    assert parsed["X-Injected"] is None
    assert parsed["To"] == "ap@harlowbakery.example"
    assert parsed["Subject"] == "Subject line X-Injected: yes"
    assert (
        gmail_module.format_recipients("a@x.example, Bee <b@y.example>, nonsense") == "a@x.example, Bee <b@y.example>"
    )


def test_gmail_draft_action_shape() -> None:
    action = gmail.draft_action(
        "a5",
        "ap@harlowbakery.example",
        "Confirmation: renewal notices for Harlow Bakery",
        "Renewal notices now go to ap@harlowbakery.example (previously billing@harlowbakery.example).",
        ("deliverable[unsent_customer_confirmation]",),
        target_refs=("company:9001",),
    )
    assert action is not None
    assert (action.kind, action.provider, action.method, action.path) == ("draft", "gmail", "POST", DRAFTS_PATH)
    assert action.body_encoding == "json"
    assert action.fields == ("message", "raw")
    assert action.headers == {}
    assert action.target_refs == ("company:9001", "recipient:ap@harlowbakery.example")
    body = cast(dict[str, dict[str, str]], action.body)
    assert set(body) == {"message"} and set(body["message"]) == {"raw"}
    parsed = parse_rfc2822(body["message"]["raw"])
    assert parsed["To"] == "ap@harlowbakery.example"
    assert parsed["Subject"] == "Confirmation: renewal notices for Harlow Bakery"
    assert "previously billing@harlowbakery.example" in parsed.get_content()
    assert action.readback is not None
    assert action.readback.method == "GET"
    assert action.readback.path == f"{DRAFTS_PATH}/{CREATED_ID_PLACEHOLDER}"
    assert action.readback.query == {"format": "full"}
    assert action.readback.field_path == "message.snippet"
    assert fill_placeholders(action.readback, {"id": "r-7781"}).path == f"{DRAFTS_PATH}/r-7781"

    assert gmail.draft_action("a6", "not-an-address", "s", "b", ("d[1]",)) is None
    assert gmail.draft_action("a6", "ap@harlowbakery.example", "", "  ", ("d[1]",)) is None
    assert gmail.draft_action("", "ap@harlowbakery.example", "s", "b", ("d[1]",)) is None


async def test_gmail_list_drafts_decodes_each_draft() -> None:
    bus, gateway = make_bus(
        Route("GET", DRAFTS_PATH, fixture("gmail_drafts_list")),
        Route("GET", f"{DRAFTS_PATH}/r-7781", fixture("gmail_draft_full")),
    )
    drafts = await gmail.list_drafts(bus)
    assert len(drafts) == 1
    draft = drafts[0]
    assert draft["id"] == "r-7781"
    assert draft["to"] == "ap@harlowbakery.example"
    assert draft["subject"] == "Confirmation: renewal notice address for Harlow Bakery"
    assert cast(str, draft["body"]).startswith("This confirms that renewal notices for Harlow Bakery")
    assert draft["labels"] == ["DRAFT"]
    (listing,) = gateway.to(DRAFTS_PATH)
    assert listing["query"] == {"maxResults": "50"}
    (get,) = gateway.to(f"{DRAFTS_PATH}/r-7781")
    assert get["query"] == {"format": "full"}
    assert all(call["method"] == "GET" for call in gateway.calls)


async def test_gmail_policy_sources_read_every_message() -> None:
    bus, gateway = make_bus(
        Route("GET", MESSAGES_PATH, fixture("gmail_messages_list")),
        Route("GET", f"{MESSAGES_PATH}/msg_a1f3e9c2b7d4", fixture("gmail_message_multipart")),
        Route("GET", f"{MESSAGES_PATH}/msg_b2c4d6e8f0a1", fixture("gmail_message_plain")),
        phase="P1",
    )
    sources = await gmail.policy_sources(bus, TaskFrame(originating_channel="billing-desk"))
    assert [source.resource_ref for source in sources] == ["message:msg_a1f3e9c2b7d4", "message:msg_b2c4d6e8f0a1"]
    assert sources[0].title == "Policy: customer confirmations require owner review"
    assert sources[0].author == "Ops Policy <ops-policy@brightline.example>"
    assert sources[0].text.startswith(
        "Policy: customer confirmations require owner review\nReminder from operations policy:"
    )
    assert sources[0].path == f"{MESSAGES_PATH}/msg_a1f3e9c2b7d4"
    (listing,) = gateway.to(MESSAGES_PATH)
    assert listing["query"] == {"maxResults": "50"}
    assert all(call["query"] == {"format": "full"} for call in gateway.calls[1:])
    assert len(gateway.calls) == 3


async def test_gmail_policy_sweep_stops_at_the_phase_budget() -> None:
    ids = [{"id": f"msg_{index:04d}", "threadId": f"thr_{index:04d}"} for index in range(10)]
    routes = [Route("GET", MESSAGES_PATH, {"messages": ids, "resultSizeEstimate": 10})]
    routes += [Route("GET", f"{MESSAGES_PATH}/msg_{index:04d}", fixture("gmail_message_plain")) for index in range(10)]
    bus, gateway = make_bus(*routes, phase="P0")  # P0 allows six provider calls
    sources = await gmail.policy_sources(bus, TaskFrame())
    assert len(gateway.calls) == 6
    assert len(sources) == 5


async def test_gmail_policy_sweep_caps_reads_at_twenty_five() -> None:
    ids = [{"id": f"msg_{index:04d}", "threadId": f"thr_{index:04d}"} for index in range(40)]
    routes = [Route("GET", MESSAGES_PATH, {"messages": ids, "resultSizeEstimate": 40})]
    routes += [Route("GET", f"{MESSAGES_PATH}/msg_{index:04d}", fixture("gmail_message_plain")) for index in range(40)]
    bus, gateway = make_bus(*routes, phase="P5")  # forty calls available
    sources = await gmail.policy_sources(bus, TaskFrame())
    assert len(gateway.calls) == 1 + 25
    assert len(sources) == 25


async def test_gmail_search_messages_uses_q() -> None:
    bus, gateway = make_bus(
        Route("GET", MESSAGES_PATH, fixture("gmail_messages_list")),
        Route("GET", f"{MESSAGES_PATH}/msg_a1f3e9c2b7d4", fixture("gmail_message_multipart")),
        Route("GET", f"{MESSAGES_PATH}/msg_b2c4d6e8f0a1", fixture("gmail_message_plain")),
    )
    found = await gmail.search_messages(bus, "ap@harlowbakery.example")
    assert len(found) == 2
    (listing,) = gateway.to(MESSAGES_PATH)
    assert listing["query"] == {"maxResults": "10", "q": "ap@harlowbakery.example"}
    assert await gmail.search_messages(bus, "   ") == []
    assert await gmail.find_candidates(bus, "Harlow Bakery", ()) == []


async def test_gmail_read_record_and_failed_reads() -> None:
    bus, _ = make_bus(
        Route("GET", f"{DRAFTS_PATH}/r-7781", fixture("gmail_draft_full")),
        Route("GET", f"{MESSAGES_PATH}/msg_b2c4d6e8f0a1", fixture("gmail_message_plain")),
        Route("GET", MESSAGES_PATH, {"error": "boom"}, status=500),
    )
    draft = await gmail.read_record(bus, "draft:r-7781")
    assert draft is not None
    assert draft.resource_type == "draft"
    assert draft.fields["labels"] == "DRAFT"
    assert draft.fields["to"] == "ap@harlowbakery.example"
    assert await gmail.read_field(bus, "message:msg_b2c4d6e8f0a1", "subject") == "Renewal notices for Harlow Bakery"
    assert await gmail.read_record(bus, "message:unknown") is None
    assert await gmail.read_record(bus, "label:INBOX") is None
    assert await gmail.list_message_ids(bus) == []
    assert await gmail.policy_sources(bus, TaskFrame()) == []


def test_gmail_playbook_never_offers_a_send_or_label_mutation() -> None:
    source = inspect.getsource(gmail_module)
    for fragment in ("/send", "messages/send", "drafts/send", "/modify", "/trash", "/labels", "addLabelIds"):
        assert fragment not in source, fragment
    action = gmail.draft_action("a1", "ap@harlowbakery.example", "s", "b", ("d[1]",))
    assert action is not None
    assert action.method == "POST" and action.path == DRAFTS_PATH
    assert gmail.update_action("a2", "message:m1", {"labelIds": "SENT"}, ("d[1]",)) is None
    assert gmail.message_action("a3", "C1", "text", ("d[1]",)) is None


# --------------------------------------------------------------------------------------
# HubSpot
# --------------------------------------------------------------------------------------


async def test_hubspot_find_candidates_search_shapes() -> None:
    bus, gateway = make_bus(
        Route("POST", "/crm/v3/objects/companies/search", fixture("hubspot_companies_search")),
        Route("POST", "/crm/v3/objects/contacts/search", fixture("hubspot_contacts_search")),
    )
    candidates = await hubspot.find_candidates(
        bus, "Harlow Bakery", ("ap@harlowbakery.example", "harlowbakery.example", "9001")
    )

    (company_search,) = gateway.to("/crm/v3/objects/companies/search")
    assert company_search["method"] == "POST"
    assert company_search["body_encoding"] == "json"
    body = cast(dict[str, Any], company_search["body"])
    assert body["properties"] == ["name", "domain", "description", "lifecyclestage", "hs_lastmodifieddate"]
    assert body["limit"] == 100
    filters = [group["filters"][0] for group in body["filterGroups"]]
    assert filters == [
        {"propertyName": "name", "operator": "EQ", "value": "Harlow Bakery"},
        {"propertyName": "name", "operator": "CONTAINS_TOKEN", "value": "bakery"},
        {"propertyName": "name", "operator": "CONTAINS_TOKEN", "value": "harlow"},
        {"propertyName": "domain", "operator": "EQ", "value": "harlowbakery.example"},
        {"propertyName": "domain", "operator": "CONTAINS_TOKEN", "value": "harlowbakery.example"},
    ]

    contact_searches = gateway.to("/crm/v3/objects/contacts/search")
    assert len(contact_searches) == 2  # eight groups, five per call
    first = cast(dict[str, Any], contact_searches[0]["body"])
    assert first["properties"] == ["email", "firstname", "lastname", "lifecyclestage", "notes", "company"]
    contact_filters = [group["filters"][0] for group in first["filterGroups"]]
    assert contact_filters[0] == {"propertyName": "email", "operator": "EQ", "value": "ap@harlowbakery.example"}
    assert contact_filters[1] == {
        "propertyName": "email",
        "operator": "CONTAINS_TOKEN",
        "value": "*@harlowbakery.example",
    }
    assert {group["filters"][0]["propertyName"] for group in first["filterGroups"]} == {"email", "company", "lastname"}
    assert len(gateway.calls) == 3  # the id hint 9001 was already found, so no extra lookup

    by_ref = {candidate.ref: candidate for candidate in candidates}
    assert set(by_ref) == {"company:9001", "company:9002", "contact:5501"}
    chosen = by_ref["company:9001"]
    assert (chosen.display, chosen.domain, chosen.lifecycle) == ("Harlow Bakery", "harlowbakery.example", "customer")
    assert "ap@harlowbakery.example" in chosen.notes
    lookalike = by_ref["company:9002"]
    assert (lookalike.display, lookalike.domain, lookalike.lifecycle) == (
        "Harlow Bakeries Prospect",
        "harlow-bakeries.example",
        "lead",
    )
    contact = by_ref["contact:5501"]
    assert (contact.display, contact.email, contact.domain) == (
        "Dana Okafor",
        "ap@harlowbakery.example",
        "harlowbakery.example",
    )
    assert "company: Harlow Bakery" in contact.notes


async def test_hubspot_falls_back_to_a_plain_list_when_search_is_empty() -> None:
    bus, gateway = make_bus(
        Route("POST", "/crm/v3/objects/companies/search", fixture("hubspot_search_empty")),
        Route("GET", "/crm/v3/objects/companies", fixture("hubspot_companies_list")),
        Route("POST", "/crm/v3/objects/contacts/search", fixture("hubspot_search_empty")),
    )
    candidates = await hubspot.find_candidates(bus, "Harlow Bakery", ())
    (listing,) = gateway.to("/crm/v3/objects/companies", "GET")
    assert listing["query"] == {
        "limit": "100",
        "properties": "name,domain,description,lifecyclestage,hs_lastmodifieddate",
    }
    assert {candidate.ref for candidate in candidates} == {"company:9001", "company:9002"}  # Cobalt Ridge filtered out
    assert gateway.to("/crm/v3/objects/contacts", "GET") == []  # no email or domain hints: no contact fallback
    # company search + company list + two contact searches (six token groups, five per call)
    assert len(gateway.calls) == 4


async def test_hubspot_falls_back_when_search_fails_and_looks_up_id_hints() -> None:
    bus, gateway = make_bus(
        Route("GET", "/crm/v3/objects/companies", fixture("hubspot_companies_list")),
        Route("GET", "/crm/v3/objects/contacts", {"results": []}),
        Route("GET", "/crm/v3/objects/companies/9003", fixture("hubspot_company_get")),
    )
    candidates = await hubspot.find_candidates(bus, "Harlow Bakery", ("9003", "cus_notahubspotid"))
    assert {candidate.ref for candidate in candidates} == {"company:9001", "company:9002"}
    assert len(gateway.to("/crm/v3/objects/companies/9003")) == 1
    assert len(gateway.to("/crm/v3/objects/contacts", "GET")) == 1  # search endpoint failed, so list
    assert len(gateway.calls) <= 8


async def test_hubspot_find_candidates_is_bounded() -> None:
    bus, gateway = make_bus()  # every call 404s
    hints = ("alpha", "beta", "gamma", "delta", "x@one.example", "y@two.example", "three.example", "101", "202", "303")
    assert await hubspot.find_candidates(bus, "Harlow Bakery Holdings Group", hints) == []
    assert len(gateway.calls) <= 8
    assert await hubspot.find_candidates(bus, "   ", ()) == []


def test_hubspot_filter_groups_are_capped() -> None:
    terms = entity_terms(
        "Alpha Beta Gamma Delta Epsilon", ["one.example", "two.example", "three.example"], max_tokens=6
    )
    assert len(company_filter_groups(terms)) <= 10
    assert len(contact_filter_groups(terms)) <= 10


def test_hubspot_update_action_shape() -> None:
    action = hubspot.update_action(
        "a3",
        "company:9001",
        {"description": "Renewal notices go to ap@harlowbakery.example", "lifecyclestage": "customer"},
        ("end_state[0]",),
        target_refs=("Harlow Bakery",),
        rationale="verified contact",
    )
    assert action is not None
    assert (action.kind, action.provider, action.method) == ("update", "hubspot", "PATCH")
    assert action.path == "/crm/v3/objects/companies/9001"
    assert action.body == {
        "properties": {"description": "Renewal notices go to ap@harlowbakery.example", "lifecyclestage": "customer"}
    }
    assert action.body_encoding == "json"
    assert action.fields == ("description", "lifecyclestage")
    assert action.target_refs == ("Harlow Bakery", "company:9001")
    assert action.rationale == "verified contact"
    assert action.readback == ReadBack(
        method="GET",
        path="/crm/v3/objects/companies/9001",
        query={"properties": "description,lifecyclestage"},
        field_path="properties.description",
    )
    plural = hubspot.update_action("a4", "contacts:5501", {"email": "ap@harlowbakery.example"}, ("end_state[1]",))
    assert plural is not None
    assert plural.path == "/crm/v3/objects/contacts/5501"
    assert plural.target_refs == ("contact:5501",)
    assert hubspot.update_action("a5", "widget:1", {"name": "x"}, ("e",)) is None
    assert hubspot.update_action("a5", "company:9001", {}, ("e",)) is None
    assert hubspot.update_action("a5", "company", {"name": "x"}, ("e",)) is None


async def test_hubspot_read_record_and_read_field() -> None:
    bus, gateway = make_bus(Route("GET", "/crm/v3/objects/companies/9001", fixture("hubspot_company_get")))
    record = await hubspot.read_record(bus, "company:9001")
    assert record is not None
    assert (record.resource_type, record.resource_id) == ("company", "9001")
    assert record.fields["name"] == "Harlow Bakery"
    assert record.fields["domain"] == "harlowbakery.example"
    assert record.fields["lifecyclestage"] == "customer"
    assert gateway.calls[0]["query"] == {"properties": "name,domain,description,lifecyclestage,hs_lastmodifieddate"}

    assert await hubspot.read_field(bus, "companies:9001", "description") == record.fields["description"]
    assert gateway.calls[1]["query"] == {"properties": "description"}
    assert await hubspot.read_field(bus, "company:9001", "") is None
    assert await hubspot.read_record(bus, "company:404") is None


async def test_hubspot_record_texts_become_policy_sources() -> None:
    bus, _ = make_bus(
        Route("POST", "/crm/v3/objects/companies/search", fixture("hubspot_companies_search")),
        Route("POST", "/crm/v3/objects/contacts/search", fixture("hubspot_contacts_search")),
        phase="P1",
    )
    frame = TaskFrame(subject_entities=("Harlow Bakery",), observed_identifiers=("ap@harlowbakery.example",))
    sources = await hubspot.policy_sources(bus, frame)
    by_ref = {source.resource_ref: source for source in sources}
    assert set(by_ref) == {"company:9001", "company:9002", "contact:5501"}
    assert by_ref["company:9001"].title == "Harlow Bakery"
    assert "account owner review" in by_ref["company:9001"].text
    assert by_ref["company:9001"].path == "/crm/v3/objects/companies/9001"
    assert all(isinstance(source, PolicySource) and source.provider == "hubspot" for source in sources)
    assert await hubspot.policy_sources(bus, TaskFrame()) == []


# --------------------------------------------------------------------------------------
# Stripe
# --------------------------------------------------------------------------------------


async def test_stripe_find_candidates_search_query() -> None:
    bus, gateway = make_bus(Route("GET", "/v1/customers/search", fixture("stripe_customers_search")))
    candidates = await stripe.find_candidates(bus, "Harlow Bakery", ("billing@harlowbakery.example",))
    (search,) = gateway.to("/v1/customers/search")
    assert search["method"] == "GET"
    assert search["query"] == {
        "query": (
            "name~'Harlow Bakery' OR name~'bakery' OR name~'harlow' "
            "OR email~'harlowbakery.example' OR email~'billing@harlowbakery.example'"
        ),
        "limit": "100",
    }
    assert len(gateway.calls) == 1
    by_ref = {candidate.ref: candidate for candidate in candidates}
    assert set(by_ref) == {"customer:cus_HB001a2b3c4d5", "customer:cus_HB002e6f7g8h9"}
    chosen = by_ref["customer:cus_HB001a2b3c4d5"]
    assert (chosen.display, chosen.email, chosen.domain, chosen.lifecycle) == (
        "Harlow Bakery",
        "billing@harlowbakery.example",
        "harlowbakery.example",
        "customer",
    )
    assert "crm_id=9001" in chosen.notes
    lookalike = by_ref["customer:cus_HB002e6f7g8h9"]
    assert lookalike.lifecycle == "prospect"  # from the description, no metadata
    assert lookalike.domain == "harlow-bakeries.example"


async def test_stripe_falls_back_to_list_with_pagination() -> None:
    bus, gateway = make_bus(
        Route("GET", "/v1/customers", fixture("stripe_customers_list_page1"), where=no_starting_after),
        Route(
            "GET",
            "/v1/customers",
            fixture("stripe_customers_list_page2"),
            query={"starting_after": "cus_CR003i0j1k2l3"},
        ),
        Route("GET", "/v1/customers/cus_HB001a2b3c4d5", fixture("stripe_customer_get")),
    )
    candidates = await stripe.find_candidates(bus, "Harlow Bakery", ("cus_HB001a2b3c4d5",))
    assert len(gateway.to("/v1/customers/search")) == 1  # tried, 404 → fallback
    pages = gateway.to("/v1/customers", "GET")
    assert [page["query"] for page in pages] == [
        {"limit": "100"},
        {"limit": "100", "starting_after": "cus_CR003i0j1k2l3"},
    ]
    assert {candidate.ref for candidate in candidates} == {"customer:cus_HB001a2b3c4d5", "customer:cus_HB002e6f7g8h9"}
    assert gateway.to("/v1/customers/cus_HB001a2b3c4d5") == []  # id hint already found via the list
    assert len(gateway.calls) <= 8


async def test_stripe_id_hint_is_read_directly() -> None:
    bus, gateway = make_bus(
        Route("GET", "/v1/customers/search", {"object": "search_result", "data": [], "has_more": False}),
        Route("GET", "/v1/customers", {"object": "list", "data": [], "has_more": False}),
        Route("GET", "/v1/customers/cus_HB001a2b3c4d5", fixture("stripe_customer_get")),
    )
    candidates = await stripe.find_candidates(bus, "Harlow Bakery", ("cus_HB001a2b3c4d5",))
    assert [candidate.ref for candidate in candidates] == ["customer:cus_HB001a2b3c4d5"]
    assert len(gateway.to("/v1/customers/cus_HB001a2b3c4d5")) == 1


def test_stripe_search_clause_quoting() -> None:
    assert quote("O'Neil & Sons") == "'O\\'Neil & Sons'"
    terms = entity_terms("Harlow Bakery", ["harlow-bakeries.example"])
    assert search_clauses(terms) == [
        "name~'Harlow Bakery'",
        "name~'bakery'",
        "name~'harlow'",
        "email~'harlow-bakeries.example'",
    ]


def test_stripe_update_action_form_shape() -> None:
    action = stripe.update_action(
        "a2",
        "customer:cus_HB001a2b3c4d5",
        {"email": "ap@harlowbakery.example"},
        ("end_state[0]",),
        target_refs=("Harlow Bakery",),
    )
    assert action is not None
    assert (action.kind, action.provider, action.method) == ("update", "stripe", "POST")
    assert action.path == "/v1/customers/cus_HB001a2b3c4d5"
    assert action.body == {"email": "ap@harlowbakery.example"}
    assert action.body_encoding == "form"
    key = action.headers["Idempotency-Key"]
    body = cast(dict[str, object], action.body)
    assert key.startswith("bp-a2-") and len(key) == len("bp-a2-") + 16
    assert key == idempotency_key("a2", action.path, body)  # same write, same key: retries stay idempotent
    assert key != idempotency_key("a2", action.path + "x", body)  # a different customer gets its own key
    assert action.fields == ("email",)
    assert action.target_refs == ("Harlow Bakery", "customer:cus_HB001a2b3c4d5")
    assert action.readback == ReadBack(method="GET", path="/v1/customers/cus_HB001a2b3c4d5", field_path="email")
    assert extract_field(fixture("stripe_customer_get"), "email") == "billing@harlowbakery.example"

    nested = stripe.update_action("a3", "customers:cus_HB001a2b3c4d5", {"metadata.lifecycle": "customer"}, ("e",))
    assert nested is not None
    assert nested.body == {"metadata[lifecycle]": "customer"}
    assert nested.fields == ("metadata[lifecycle]",)
    assert nested.readback is not None and nested.readback.field_path == "metadata.lifecycle"
    assert extract_field(fixture("stripe_customer_get"), "metadata.lifecycle") == "customer"


@pytest.mark.parametrize(
    "fields",
    [
        {"balance": "-500"},
        {"coupon": "SAVE50"},
        {"default_source": "card_1"},
        {"email": "ap@harlowbakery.example", "invoice_settings[footer]": "x"},
        {"tax_exempt": "exempt"},
        {},
    ],
)
def test_stripe_update_action_refuses_billing_configuration(fields: dict[str, str]) -> None:
    assert stripe.update_action("a4", "customer:cus_HB001a2b3c4d5", fields, ("e",)) is None


def test_stripe_update_action_rejects_other_resources() -> None:
    assert stripe.update_action("a5", "subscription:sub_1", {"email": "x@y.example"}, ("e",)) is None
    assert stripe.update_action("a5", "customer", {"email": "x@y.example"}, ("e",)) is None
    assert stripe.update_action("", "customer:cus_1", {"email": "x@y.example"}, ("e",)) is None


async def test_stripe_read_record_flattens_metadata() -> None:
    bus, gateway = make_bus(Route("GET", "/v1/customers/cus_HB001a2b3c4d5", fixture("stripe_customer_get")))
    record = await stripe.read_record(bus, "customer:cus_HB001a2b3c4d5")
    assert record is not None
    assert (record.resource_type, record.resource_id) == ("customer", "cus_HB001a2b3c4d5")
    assert record.fields["email"] == "billing@harlowbakery.example"
    assert record.fields["metadata.lifecycle"] == "customer"
    assert record.fields["metadata"] == "lifecycle=customer; crm_id=9001"
    assert gateway.calls[0]["path"] == "/v1/customers/cus_HB001a2b3c4d5"
    assert "query" not in gateway.calls[0]
    assert await stripe.read_field(bus, "customer:cus_HB001a2b3c4d5", "name") == "Harlow Bakery"
    assert await stripe.read_record(bus, "charge:ch_1") is None
    assert await stripe.read_record(bus, "customer:cus_missing") is None


async def test_stripe_record_texts_become_policy_sources() -> None:
    bus, _ = make_bus(Route("GET", "/v1/customers/search", fixture("stripe_customers_search")), phase="P1")
    frame = TaskFrame(subject_entities=("Harlow Bakery",))
    sources = await stripe.policy_sources(bus, frame)
    by_ref = {source.resource_ref: source for source in sources}
    assert set(by_ref) == {"customer:cus_HB001a2b3c4d5", "customer:cus_HB002e6f7g8h9"}
    assert by_ref["customer:cus_HB001a2b3c4d5"].text == "Bakery chain, annual plan\nlifecycle=customer; crm_id=9001"
    assert by_ref["customer:cus_HB001a2b3c4d5"].path == "/v1/customers/cus_HB001a2b3c4d5"


def test_stripe_playbook_never_offers_money_movement() -> None:
    source = inspect.getsource(stripe_module)
    for fragment in (
        "/v1/charges",
        "/v1/invoices",
        "/v1/subscriptions",
        "/v1/refunds",
        "/v1/payment_intents",
        "DELETE",
    ):
        assert fragment not in source, fragment
    assert stripe.message_action("a1", "C1", "text", ("d",)) is None
    assert stripe.draft_action("a1", "a@b.example", "s", "b", ("d",)) is None


# --------------------------------------------------------------------------------------
# Every constructed write is gate-clean by construction
# --------------------------------------------------------------------------------------


def test_constructed_actions_pass_the_gate() -> None:
    context = Context(
        trial_id="t-gate",
        user_prompt="Priya posted in #billing-desk: Harlow Bakery moved renewal notices to ap@harlowbakery.example.",
        providers=("slack", "gmail", "hubspot", "stripe"),
    )
    context.dod = DefinitionOfDone(
        forbidden=("send_email", "create_charge", "create_invoice", "update_subscription", "delete_any"),
        write_scope=("slack", "gmail", "hubspot", "stripe"),
        facts={"customer": "Harlow Bakery", "verified_contact": "ap@harlowbakery.example"},
    )
    gate = Gate(context=context, allow_unplanned=True)
    actions: list[Action | None] = [
        slack.message_action("s1", "C01BILL0001", "Harlow Bakery now bills to ap@harlowbakery.example", ("d[0]",)),
        gmail.draft_action(
            "g1", "ap@harlowbakery.example", "Confirmation for Harlow Bakery", "Now ap@harlowbakery.example", ("d[1]",)
        ),
        hubspot.update_action("h1", "company:9001", {"description": "Billing: ap@harlowbakery.example"}, ("e[0]",)),
        stripe.update_action("t1", "customer:cus_HB001a2b3c4d5", {"email": "ap@harlowbakery.example"}, ("e[1]",)),
    ]
    for action in actions:
        assert action is not None
        assert action.is_write and action.satisfies and action.readback is not None and action.fields
        verdict = gate.evaluate(action)
        assert verdict.allowed, f"{action.id} refused by {verdict.rule}: {verdict.reason}"
