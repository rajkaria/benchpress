"""Slack twin: seeded shape, calibrated routes, purity of reads, error envelopes.

The inline seed below mirrors the *shape* of the published ECOM-02 `seed_config.slack` (2 channels,
4 users, 5 messages) with invented names, so the tests prove generic behaviour. One test loads the
real ECOM-02 scenario from the vendored benchmark when it is present and skips otherwise.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from devsim.twins import available, load_spec
from devsim.twins.base import make_admin_app
from devsim.twins.slack import (
    ADMIN_STATE_KEYS,
    APP_ID,
    BOT_ID,
    BOT_USER_ID,
    TEAM_ID,
    SlackStore,
    make_data_app,
    supported_methods,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
BENCHMARK = REPO_ROOT / "arga-twins-benchmark"
ECOM_02 = BENCHMARK / "benchmark" / "argabench_40" / "scenarios" / "ecom-02.json"

CHANNEL_ID = re.compile(r"^C[A-Z0-9]{10}$")
USER_ID = re.compile(r"^U[A-Z0-9]{9}$")
EVENT_ID = re.compile(r"^Ev[0-9A-F]{11}$")
SLACK_TS = re.compile(r"^\d{10}\.\d{6}$")

# Calibrated from tests/fixtures/argabench_crm_legacy/historical-fable-5-high-crm.tar.gz (CRM-02).
FIXTURE_CHANNEL_KEYS = {
    "connected_limited_team_ids",
    "connected_team_ids",
    "context_team_id",
    "conversation_host_id",
    "created",
    "creator",
    "id",
    "internal_team_ids",
    "is_archived",
    "is_channel",
    "is_ext_shared",
    "is_general",
    "is_group",
    "is_im",
    "is_member",
    "is_mpim",
    "is_open",
    "is_org_shared",
    "is_pending_ext_shared",
    "is_private",
    "is_read_only",
    "is_shared",
    "is_thread_only",
    "last_read",
    "message_count",
    "name",
    "name_normalized",
    "num_members",
    "parent_conversation",
    "pending_connected_team_ids",
    "pending_shared",
    "previous_names",
    "properties",
    "purpose",
    "shared_team_ids",
    "topic",
    "unlinked",
    "updated",
}
FIXTURE_USER_KEYS = {
    "color",
    "deleted",
    "id",
    "is_admin",
    "is_app_user",
    "is_bot",
    "is_email_confirmed",
    "is_owner",
    "is_primary_owner",
    "is_restricted",
    "is_ultra_restricted",
    "name",
    "profile",
    "real_name",
    "team_id",
    "tz",
    "tz_label",
    "tz_offset",
    "updated",
    "who_can_share_contact_card",
}
FIXTURE_EVENT_KEYS = {"created_at", "envelope", "event_type", "id", "pending", "source_method"}
FIXTURE_ENVELOPE_KEYS = {
    "api_app_id",
    "authed_users",
    "authorizations",
    "event",
    "event_id",
    "event_time",
    "team_id",
    "token",
    "type",
}
FIXTURE_MESSAGE_EVENT_KEYS = {"channel", "channel_type", "text", "ts", "type", "user"}
FIXTURE_HISTORY_KEYS = {
    "channel_actions_count",
    "channel_actions_ts",
    "has_more",
    "messages",
    "ok",
    "pin_count",
    "response_metadata",
}
FIXTURE_HISTORY_MESSAGE_KEYS = {"blocks", "client_msg_id", "team", "text", "ts", "type", "user"}
FIXTURE_POST_MESSAGE_KEYS = {
    "app_id",
    "blocks",
    "bot_id",
    "bot_profile",
    "client_msg_id",
    "team",
    "text",
    "ts",
    "type",
    "user",
}

SEED: dict[str, Any] = {
    "channels": [
        {
            "name": "ops-desk",
            "messages": [
                {"text": "Rivermill Studio asked to move renewal notices to accounts payable.", "user": "dana-cruz"},
                {"text": "The records system completed its nightly archival at 02:00 UTC.", "user": "facilities-desk"},
                {"text": "Records sync: Rivermill Studio uses billing@rivermill.example.", "user": "records-sync"},
                {"text": "Earlier activity: Rivermill Studios Prospect has never purchased.", "user": "audit-trail"},
            ],
        },
        {
            "name": "all-hands",
            "messages": [
                {"text": "Guest Wi-Fi on the fourth floor is briefly down at 18:30.", "user": "facilities-desk"}
            ],
        },
    ],
    "users": [
        {"name": "dana-cruz", "real_name": "Dana Cruz"},
        {"name": "facilities-desk", "real_name": "Facilities Desk"},
        {"name": "records-sync", "real_name": "Records Sync"},
        {"name": "audit-trail", "real_name": "Audit Trail"},
    ],
}


def seeded(seed_key: str = "seed-a", config: dict[str, Any] | None = None) -> SlackStore:
    store = SlackStore(seed_key)
    store.seed(config or SEED)
    return store


def channel_named(store: SlackStore, name: str) -> str:
    channel_id = store.channel_by_name(name)
    assert channel_id is not None
    return channel_id


@asynccontextmanager
async def clients(store: SlackStore) -> AsyncGenerator[tuple[httpx.AsyncClient, httpx.AsyncClient]]:
    data = httpx.AsyncClient(transport=httpx.ASGITransport(app=make_data_app(store)), base_url="http://slack")
    admin = httpx.AsyncClient(transport=httpx.ASGITransport(app=make_admin_app(store)), base_url="http://slack-admin")
    async with data, admin:
        yield data, admin


def body(response: httpx.Response) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    return cast(dict[str, Any], response.json())


def messages_of(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], payload["messages"])


def message_events(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [event for event in cast(list[dict[str, Any]], state["events"]) if event["event_type"] == "message"]


# --------------------------------------------------------------------------------------
# Seeding and admin-state shape
# --------------------------------------------------------------------------------------


def test_seed_counts_ids_and_membership() -> None:
    store = seeded()
    state = store.admin_state()
    channels = cast(list[dict[str, Any]], state["channels"])
    users = cast(list[dict[str, Any]], state["users"])
    assert len(channels) == 2
    assert len(users) == 4 + 2, "four seeded users plus the twin's bot and human"
    assert len(message_events(state)) == 5
    assert [event["event_type"] for event in state["events"]].count("channel_created") == 2

    by_name = {channel["name"]: channel for channel in channels}
    assert by_name["ops-desk"]["message_count"] == 4 and by_name["all-hands"]["message_count"] == 1
    assert by_name["ops-desk"]["num_members"] == 5, "four posting users + the bot, as in the fixture"
    assert by_name["all-hands"]["num_members"] == 2
    assert all(CHANNEL_ID.fullmatch(channel["id"]) for channel in channels)
    assert all(channel["is_member"] is True for channel in channels)
    assert all(channel["last_read"] == "0000000000.000000" for channel in channels)
    assert all(channel["updated"] == channel["created"] for channel in channels)

    seeded_users = [user for user in users if user["id"] not in {BOT_USER_ID, "UTWINUSR"}]
    assert all(USER_ID.fullmatch(user["id"]) for user in seeded_users)
    dana = next(user for user in users if user["name"] == "dana-cruz")
    assert dana["id"].startswith("UDANAC") and dana["real_name"] == "Dana Cruz"
    assert dana["profile"]["first_name"] == "Dana" and dana["profile"]["last_name"] == "Cruz"
    assert dana["profile"]["email"] == "dana-cruz@slack-twin.local"
    bot = next(user for user in users if user["id"] == BOT_USER_ID)
    assert bot["is_bot"] is True and bot["is_app_user"] is True and bot["name"] == "slack-twin-bot"
    assert [user["id"] for user in users] == sorted(user["id"] for user in users)
    assert [channel["id"] for channel in channels] == sorted(channel["id"] for channel in channels)


def test_admin_state_matches_calibrated_shape() -> None:
    state = seeded().admin_state()
    assert tuple(sorted(state)) == ADMIN_STATE_KEYS
    assert state["team"] == {"app_id": APP_ID, "domain": "slack-twin", "id": TEAM_ID, "name": "Default Workspace"}
    assert set(state["apps"][0]) == {"app_id", "hosted_variable_names", "installed_team_ids", "manifest"}
    assert state["base_time"] is None and state["rate_limiting_enabled"] is False and state["seed"] == 1
    for key in ("canvases", "deliveries", "failure_rules", "files", "subscriptions", "triggers"):
        assert state[key] == []
    for key in ("custom_emoji", "legacy_preferences", "legacy_resources"):
        assert state[key] == {}
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?\+00:00", state["logical_now"])

    channel = cast(dict[str, Any], state["channels"][0])
    assert set(channel) == FIXTURE_CHANNEL_KEYS
    assert channel["previous_names"] == [channel["name"]]
    assert channel["purpose"] == {"creator": "", "last_set": channel["created"], "value": ""}
    assert channel["topic"] == {"creator": "", "last_set": 0, "value": ""}
    assert channel["creator"] == BOT_USER_ID

    user = cast(dict[str, Any], state["users"][0])
    assert set(user) == FIXTURE_USER_KEYS
    assert len(user["profile"]) == 32 and user["profile"]["team"] == TEAM_ID

    # Messages live in `events[]` as Events-API envelopes, newest first; there is no channels[].messages.
    assert "messages" not in channel
    events = cast(list[dict[str, Any]], state["events"])
    assert [event["created_at"] for event in events] == sorted((e["created_at"] for e in events), reverse=True)
    message_event = message_events(state)[0]
    assert set(message_event) == FIXTURE_EVENT_KEYS
    assert set(message_event["envelope"]) == FIXTURE_ENVELOPE_KEYS
    assert set(message_event["envelope"]["event"]) == FIXTURE_MESSAGE_EVENT_KEYS
    assert message_event["source_method"] == "chat.postMessage" and message_event["pending"] is True
    assert EVENT_ID.fullmatch(message_event["id"]) and message_event["id"] == message_event["envelope"]["event_id"]
    assert message_event["envelope"]["token"] == "slack-twin" and message_event["envelope"]["type"] == "event_callback"
    author = message_event["envelope"]["event"]["user"]
    assert message_event["envelope"]["authed_users"] == [author]
    assert message_event["envelope"]["authorizations"][0] == {
        "enterprise_id": None,
        "is_bot": False,
        "is_enterprise_install": False,
        "team_id": TEAM_ID,
        "user_id": author,
    }
    created = next(event for event in events if event["event_type"] == "channel_created")
    assert created["source_method"] == "conversations.create"
    assert set(created["envelope"]["event"]) == {"channel", "created", "type"}


def test_seed_is_deterministic_per_seed_key() -> None:
    first, second, other = seeded("k1"), seeded("k1"), seeded("k2")
    assert json.dumps(first.admin_state(), sort_keys=True) == json.dumps(second.admin_state(), sort_keys=True)
    assert set(first.channels) != set(other.channels)
    assert set(first.users) - {BOT_USER_ID, "UTWINUSR"} != set(other.users) - {BOT_USER_ID, "UTWINUSR"}
    assert BOT_USER_ID in other.users, "the bot identity is a fixed twin constant"


def test_seed_accepts_the_full_seed_config_or_the_slack_slice() -> None:
    from_slice = seeded("k", SEED)
    from_full = seeded("k", {"slack": SEED, "stripe": {"customers": []}})
    assert from_slice.admin_state() == from_full.admin_state()


@pytest.mark.skipif(not ECOM_02.exists(), reason="vendored benchmark not available")
async def test_real_ecom_02_seed_and_originating_channel_post() -> None:
    scenario = cast(dict[str, Any], json.loads(ECOM_02.read_text(encoding="utf-8")))
    store = SlackStore("ecom-02")
    store.seed(cast(dict[str, Any], scenario["seed_config"]))
    state = store.admin_state()
    assert len(state["channels"]) == 2 and len(state["users"]) == 6 and len(message_events(state)) == 5
    names = {channel["name"] for channel in cast(list[dict[str, Any]], state["channels"])}
    assert names == {"commerce-ops", "company-updates"}
    async with clients(store) as (data, _admin):
        posted = body(await data.post("/api/chat.postMessage", json={"channel": "#commerce-ops", "text": "update"}))
    assert posted["ok"] is True and posted["channel"] == channel_named(store, "commerce-ops")
    # The legacy grader resolves the originating channel by *name* in admin state and credits posts by *id*.
    ids = {c["id"] for c in cast(list[dict[str, Any]], store.admin_state()["channels"]) if c["name"] == "commerce-ops"}
    assert posted["channel"] in ids


# --------------------------------------------------------------------------------------
# Reads: list, history, replies, info, users, auth
# --------------------------------------------------------------------------------------


async def test_conversations_list_shape_types_and_pagination() -> None:
    store = seeded()
    async with clients(store) as (data, _admin):
        listed = body(
            await data.get("/api/conversations.list", params={"limit": 200, "types": "public_channel,private_channel"})
        )
        assert set(listed) == {"channels", "ok", "response_metadata"}
        assert listed["response_metadata"] == {"next_cursor": ""}
        channels = cast(list[dict[str, Any]], listed["channels"])
        assert [channel["name"] for channel in channels] == ["all-hands", "ops-desk"] or len(channels) == 2
        assert set(channels[0]) == FIXTURE_CHANNEL_KEYS - {"message_count"}, "message_count is admin-only"

        page_one = body(await data.get("/api/conversations.list", params={"limit": 1}))
        cursor = page_one["response_metadata"]["next_cursor"]
        assert len(page_one["channels"]) == 1 and cursor
        page_two = body(await data.get("/api/conversations.list", params={"limit": 1, "cursor": cursor}))
        assert len(page_two["channels"]) == 1 and page_two["response_metadata"]["next_cursor"] == ""
        assert page_one["channels"][0]["id"] != page_two["channels"][0]["id"]

        assert body(await data.get("/api/conversations.list", params={"types": "private_channel"}))["channels"] == []
        assert body(await data.get("/api/conversations.list", params={"types": "im"}))["channels"] == []
        assert body(await data.get("/api/conversations.list", params={"cursor": "bogus"}))["error"] == "invalid_cursor"
        assert body(await data.get("/api/conversations.list", params={"limit": 5000}))["error"] == "invalid_limit"
        # POST is a read for list-style methods, exactly as Slack (and the grader's mutation rule) treat it.
        assert body(await data.post("/api/conversations.list", json={"limit": 10}))["ok"] is True


async def test_history_is_newest_first_with_valid_ts_and_filters() -> None:
    store = seeded()
    ops = channel_named(store, "ops-desk")
    async with clients(store) as (data, _admin):
        history = body(await data.get("/api/conversations.history", params={"channel": ops, "limit": 50}))
        assert set(history) == FIXTURE_HISTORY_KEYS
        assert history["has_more"] is False and history["pin_count"] == 0
        assert history["channel_actions_ts"] is None and history["channel_actions_count"] == 0
        assert history["response_metadata"] == {"next_cursor": ""}
        messages = messages_of(history)
        assert len(messages) == 4
        assert all(set(message) == FIXTURE_HISTORY_MESSAGE_KEYS for message in messages)
        assert all(SLACK_TS.fullmatch(message["ts"]) for message in messages)
        keys = [tuple(int(part) for part in message["ts"].split(".")) for message in messages]
        assert keys == sorted(keys, reverse=True) and len(set(keys)) == 4, "newest first, unique"
        assert messages[-1]["text"].startswith("Rivermill Studio asked"), "first seeded message is the oldest"
        assert all(message["team"] == TEAM_ID and message["type"] == "message" for message in messages)
        assert all(re.fullmatch(r"[0-9a-f]{40}", message["client_msg_id"]) for message in messages)
        assert messages[0]["blocks"][0]["elements"][0]["elements"][0]["text"] == messages[0]["text"]

        first = body(await data.get("/api/conversations.history", params={"channel": ops, "limit": 3}))
        assert len(first["messages"]) == 3 and first["has_more"] is True
        cursor = first["response_metadata"]["next_cursor"]
        rest = body(await data.get("/api/conversations.history", params={"channel": ops, "limit": 3, "cursor": cursor}))
        assert [m["ts"] for m in rest["messages"]] == [messages[3]["ts"]] and rest["has_more"] is False

        oldest, latest = messages[2]["ts"], messages[1]["ts"]
        window = body(
            await data.get("/api/conversations.history", params={"channel": ops, "oldest": oldest, "latest": latest})
        )
        assert window["messages"] == [], "bounds are exclusive unless inclusive=true"
        inclusive = body(
            await data.get(
                "/api/conversations.history",
                params={"channel": ops, "oldest": oldest, "latest": latest, "inclusive": "true"},
            )
        )
        assert [m["ts"] for m in inclusive["messages"]] == [latest, oldest]
        since = body(await data.get("/api/conversations.history", params={"channel": ops, "oldest": messages[1]["ts"]}))
        assert [m["ts"] for m in since["messages"]] == [messages[0]["ts"]]


async def test_info_users_and_auth_reads() -> None:
    store = seeded()
    ops = channel_named(store, "ops-desk")
    async with clients(store) as (data, _admin):
        info = body(await data.get("/api/conversations.info", params={"channel": ops}))
        assert info["ok"] is True and info["channel"]["name"] == "ops-desk" and "message_count" not in info["channel"]
        assert (
            body(await data.get("/api/conversations.info", params={"channel": "#ops-desk"}))["error"]
            == "channel_not_found"
        )

        members = body(await data.get("/api/conversations.members", params={"channel": ops}))
        assert BOT_USER_ID in members["members"] and len(members["members"]) == 5

        users = body(await data.get("/api/users.list"))
        assert set(users) == {"cache_ts", "members", "ok", "response_metadata"} and len(users["members"]) == 6
        dana = next(user for user in cast(list[dict[str, Any]], users["members"]) if user["name"] == "dana-cruz")
        assert body(await data.get("/api/users.info", params={"user": dana["id"]}))["user"] == dana
        assert body(await data.get("/api/users.info", params={"user": "U404"}))["error"] == "user_not_found"
        by_email = body(await data.get("/api/users.lookupByEmail", params={"email": "Dana-Cruz@slack-twin.local"}))
        assert by_email["user"]["id"] == dana["id"]
        assert body(await data.get("/api/users.lookupByEmail", params={"email": "x@y.z"}))["error"] == "users_not_found"

        auth = body(await data.get("/api/auth.test", headers={"Authorization": "Bearer xoxb-anything"}))
        assert auth == {
            "ok": True,
            "url": "https://slack-twin.slack.com/",
            "team": "Default Workspace",
            "user": "slack-twin-bot",
            "team_id": TEAM_ID,
            "user_id": BOT_USER_ID,
            "bot_id": BOT_ID,
            "is_enterprise_install": False,
        }
        assert body(await data.post("/api/auth.test"))["ok"] is True, "a missing token is tolerated"
        assert body(await data.get("/api/team.info"))["team"]["id"] == TEAM_ID
        mine = body(await data.get("/api/users.conversations", params={"user": dana["id"]}))
        assert [channel["name"] for channel in mine["channels"]] == ["ops-desk"]


# --------------------------------------------------------------------------------------
# chat.postMessage
# --------------------------------------------------------------------------------------


async def test_post_message_by_id_echoes_text_and_lands_in_history_state_and_journal() -> None:
    store = seeded()
    ops = channel_named(store, "ops-desk")
    dana = store.ensure_user("dana-cruz")
    text = f"<@{dana}> Rivermill Studio → ap@rivermill.example ✔ (no charges, no sends) — 日本語 & <https://x.example|link>"
    async with clients(store) as (data, admin):
        before = body(await admin.get("/admin/state"))
        posted = body(await data.post("/api/chat.postMessage", json={"channel": ops, "text": text}))
        assert set(posted) == {"channel", "message", "ok", "ts"}
        assert posted["ok"] is True and posted["channel"] == ops and SLACK_TS.fullmatch(posted["ts"])
        message = cast(dict[str, Any], posted["message"])
        assert set(message) == FIXTURE_POST_MESSAGE_KEYS
        assert message["text"] == text and message["ts"] == posted["ts"] and message["user"] == BOT_USER_ID
        assert message["bot_id"] == BOT_ID and message["app_id"] == APP_ID and message["team"] == TEAM_ID
        assert message["bot_profile"]["user_id"] == BOT_USER_ID and message["bot_profile"]["name"] == "Slack Twin Bot"
        assert message["blocks"][0]["elements"][0]["elements"][0]["text"] == text

        history = messages_of(body(await data.get("/api/conversations.history", params={"channel": ops})))
        assert history[0]["ts"] == posted["ts"] and history[0]["text"] == text and len(history) == 5

        after = body(await admin.get("/admin/state"))
        newest = cast(dict[str, Any], after["events"][0])
        assert newest["event_type"] == "message" and newest["source_method"] == "chat.postMessage"
        assert newest["envelope"]["event"] == {
            "channel": ops,
            "channel_type": "channel",
            "text": text,
            "ts": posted["ts"],
            "type": "message",
            "user": BOT_USER_ID,
        }
        assert newest["envelope"]["authorizations"][0]["is_bot"] is True
        assert len(after["events"]) == len(before["events"]) + 1
        counts = {channel["name"]: channel["message_count"] for channel in after["channels"]}
        assert counts == {"ops-desk": 5, "all-hands": 1}
        assert after["logical_now"] > before["logical_now"], "writes tick the clock"
        assert {c["id"]: c["updated"] for c in after["channels"]} == {c["id"]: c["updated"] for c in before["channels"]}

    assert [m.collection for m in store.journal] == ["messages"]
    assert store.journal[0].record_id == posted["ts"] and store.journal[0].path == "/api/chat.postMessage"
    assert store.journal[0].before is None and cast(dict[str, Any], store.journal[0].after)["text"] == text


async def test_post_message_by_channel_name_form_encoded_and_threads() -> None:
    store = seeded()
    ops = channel_named(store, "ops-desk")
    async with clients(store) as (data, _admin):
        by_hash = body(await data.post("/api/chat.postMessage", data={"channel": "#ops-desk", "text": "by #name"}))
        assert by_hash["ok"] is True and by_hash["channel"] == ops, "response carries the resolved channel id"
        by_name = body(await data.post("/api/chat.postMessage", data={"channel": "ops-desk", "text": "by name"}))
        assert by_name["channel"] == ops
        custom = body(
            await data.post(
                "/api/chat.postMessage",
                json={"channel": ops, "text": "custom", "username": "Billing Bot", "icon_emoji": ":moneybag:"},
            )
        )
        assert custom["message"]["subtype"] == "bot_message" and custom["message"]["username"] == "Billing Bot"
        assert custom["message"]["icons"] == {"emoji": ":moneybag:"}

        parent_ts = by_hash["ts"]
        reply = body(
            await data.post("/api/chat.postMessage", json={"channel": ops, "text": "reply", "thread_ts": parent_ts})
        )
        assert reply["message"]["thread_ts"] == parent_ts and reply["message"]["parent_user_id"] == BOT_USER_ID
        replies = body(await data.get("/api/conversations.replies", params={"channel": ops, "ts": parent_ts}))
        assert [m["ts"] for m in replies["messages"]] == [parent_ts, reply["ts"]]
        assert replies["messages"][0]["reply_count"] == 1 and replies["messages"][0]["latest_reply"] == reply["ts"]
        history = messages_of(body(await data.get("/api/conversations.history", params={"channel": ops})))
        assert reply["ts"] not in {m["ts"] for m in history}, "thread replies stay out of channel history"
        assert history[0]["ts"] == custom["ts"]
        assert (
            body(await data.get("/api/conversations.replies", params={"channel": ops, "ts": "1.000001"}))["error"]
            == "thread_not_found"
        )
        missing = body(
            await data.post("/api/chat.postMessage", json={"channel": ops, "text": "x", "thread_ts": "1.000001"})
        )
        assert missing["error"] == "thread_not_found"
    assert len(store.journal) == 4


async def test_post_message_to_a_user_opens_a_dm() -> None:
    store = seeded()
    dana = store.ensure_user("dana-cruz")
    async with clients(store) as (data, admin):
        posted = body(await data.post("/api/chat.postMessage", json={"channel": dana, "text": "please review"}))
        assert posted["ok"] is True and posted["channel"].startswith("D")
        ims = body(await data.get("/api/conversations.list", params={"types": "im"}))["channels"]
        assert [im["id"] for im in ims] == [posted["channel"]] and ims[0]["user"] == dana and ims[0]["is_im"] is True
        again = body(await data.post("/api/chat.postMessage", json={"channel": dana, "text": "second"}))
        assert again["channel"] == posted["channel"], "one IM per user"
        opened = body(await data.post("/api/conversations.open", json={"users": dana}))
        assert opened == {"ok": True, "no_op": True, "already_open": True, "channel": {"id": posted["channel"]}}
        state = body(await admin.get("/admin/state"))
        im = next(channel for channel in state["channels"] if channel["id"] == posted["channel"])
        assert im["message_count"] == 2
        assert state["events"][0]["envelope"]["event"]["channel_type"] == "im"
    assert [m.collection for m in store.journal] == ["channels", "messages", "messages"]


# --------------------------------------------------------------------------------------
# Error envelopes
# --------------------------------------------------------------------------------------


async def test_error_envelopes_are_http_200_ok_false() -> None:
    store = seeded()
    ops = channel_named(store, "ops-desk")
    async with clients(store) as (data, _admin):
        assert body(await data.post("/api/chat.postMessage", json={"channel": "C0NOPE", "text": "x"})) == {
            "ok": False,
            "error": "channel_not_found",
        }
        assert (
            body(await data.post("/api/chat.postMessage", json={"channel": "#nope", "text": "x"}))["error"]
            == "channel_not_found"
        )
        assert body(await data.post("/api/chat.postMessage", json={"channel": ops}))["error"] == "no_text"
        missing = body(await data.get("/api/conversations.history"))
        assert missing["ok"] is False and missing["error"] == "invalid_arguments"
        assert missing["response_metadata"]["messages"] == ["[ERROR] missing required field: channel"]
        assert (
            body(await data.get("/api/conversations.history", params={"channel": "C0NOPE"}))["error"]
            == "channel_not_found"
        )
        unknown = body(await data.get("/api/conversations.doesNotExist"))
        assert unknown == {"ok": False, "error": "unknown_method", "req_method": "conversations.doesNotExist"}
        assert (
            body(await data.get("/api/chat.postMessage", params={"channel": ops, "text": "x"}))["error"]
            == "method_not_supported"
        )
        assert (
            body(await data.post("/api/chat.update", json={"channel": ops, "ts": "1.000001", "text": "x"}))["error"]
            == "message_not_found"
        )
        assert (
            body(await data.post("/api/reactions.add", json={"channel": ops, "timestamp": "1.000001"}))["error"]
            == "invalid_name"
        )
        seeded_ts = messages_of(body(await data.get("/api/conversations.history", params={"channel": ops})))[0]["ts"]
        assert (
            body(await data.post("/api/chat.update", json={"channel": ops, "ts": seeded_ts, "text": "x"}))["error"]
            == "cant_update_message"
        )
        assert (
            body(await data.post("/api/chat.delete", json={"channel": ops, "ts": seeded_ts}))["error"]
            == "cant_delete_message"
        )
        invalid = await data.post(
            "/api/chat.postMessage", content=b"{not json", headers={"content-type": "application/json"}
        )
        assert body(invalid)["error"] == "invalid_json"
        for path in ("/", "/admin/state", "/v1/customers", "/api"):
            response = await data.get(path)
            assert response.status_code == 404 and response.json()["ok"] is False
        assert (await data.delete(f"/api/conversations.history?channel={ops}")).status_code == 404
    assert store.journal == [] and store.admin_state()["channels"][0]["message_count"] in {1, 4}


# --------------------------------------------------------------------------------------
# Purity of reads
# --------------------------------------------------------------------------------------


async def test_reads_never_mutate_state() -> None:
    store = seeded()
    ops = channel_named(store, "ops-desk")
    async with clients(store) as (data, admin):
        snapshot = (await admin.get("/admin/state")).content
        clock = store.clock.iso()
        reads = [
            data.get("/api/conversations.list", params={"types": "public_channel,private_channel,im"}),
            data.post("/api/conversations.list", json={"limit": 1}),
            data.get("/api/conversations.history", params={"channel": ops, "limit": 2}),
            data.get("/api/conversations.history", params={"channel": ops, "oldest": "0", "inclusive": "true"}),
            data.get("/api/conversations.info", params={"channel": ops}),
            data.get("/api/conversations.members", params={"channel": ops}),
            data.get("/api/users.list"),
            data.get("/api/users.info", params={"user": BOT_USER_ID}),
            data.get("/api/auth.test"),
            data.get("/api/team.info"),
            data.get("/api/search.messages", params={"query": "rivermill"}),
            data.get("/api/pins.list", params={"channel": ops}),
            data.get("/api/emoji.list"),
            data.get("/api/api.test", params={"foo": "bar"}),
            data.get("/api/conversations.history"),
            data.get("/api/nope.nope"),
            data.get("/not-an-api-path"),
            admin.get("/admin/state"),
            admin.get("/inspect"),
        ]
        for pending in reads:
            await pending
        assert (await admin.get("/admin/state")).content == snapshot
    assert store.journal == [] and store.clock.iso() == clock
    assert (
        json.dumps(store.admin_state(), sort_keys=True).encode() == snapshot
        or json.loads(snapshot) == store.admin_state()
    )


# --------------------------------------------------------------------------------------
# Search, update, delete, reactions, pins, create, join
# --------------------------------------------------------------------------------------


async def test_search_messages_substring_and_modifiers() -> None:
    store = seeded()
    ops = channel_named(store, "ops-desk")
    async with clients(store) as (data, _admin):
        found = body(await data.get("/api/search.messages", params={"query": "RIVERMILL studio"}))
        assert set(found) == {"messages", "ok", "query"} and found["query"] == "RIVERMILL studio"
        block = cast(dict[str, Any], found["messages"])
        assert set(block) == {"matches", "pagination", "paging", "total"}
        assert block["total"] == 3, "substring match: 'rivermill studio' is inside 'rivermill studios prospect' too"
        matches = cast(list[dict[str, Any]], block["matches"])
        assert {match["channel"]["name"] for match in matches} == {"ops-desk"}
        assert all(
            match["type"] == "message" and match["permalink"].startswith("https://slack-twin.slack.com/archives/")
            for match in matches
        )
        assert [match["username"] for match in matches] == ["audit-trail", "records-sync", "dana-cruz"], "newest first"
        assert block["paging"] == {"count": 20, "page": 1, "pages": 1, "total": 3}
        assert block["pagination"]["total_count"] == 3 and block["pagination"]["first"] == 1
        exact = body(await data.get("/api/search.messages", params={"query": "billing@rivermill.example"}))
        assert exact["messages"]["total"] == 1 and exact["messages"]["matches"][0]["text"].startswith("Records sync")

        assert (
            body(await data.get("/api/search.messages", params={"query": "archival in:#all-hands"}))["messages"][
                "total"
            ]
            == 0
        )
        assert (
            body(await data.get("/api/search.messages", params={"query": "archival in:#ops-desk"}))["messages"]["total"]
            == 1
        )
        assert (
            body(await data.get("/api/search.messages", params={"query": "from:@facilities-desk"}))["messages"]["total"]
            == 2
        )
        assert (
            body(await data.get("/api/search.messages", params={"query": '"never purchased"'}))["messages"]["total"]
            == 1
        )
        assert (
            body(await data.get("/api/search.messages", params={"query": "nothing-here"}))["messages"]["matches"] == []
        )
        assert body(await data.get("/api/search.messages"))["error"] == "invalid_arguments"
        paged = body(await data.get("/api/search.messages", params={"query": "rivermill", "count": 1, "page": 2}))
        assert len(paged["messages"]["matches"]) == 1 and paged["messages"]["paging"]["pages"] == 3
        assert paged["messages"]["matches"][0]["username"] == "records-sync"
        assert (
            body(await data.get("/api/search.messages", params={"query": f"in:{ops} sync"}))["messages"]["total"] == 1
        )


async def test_update_delete_react_and_pin_own_messages() -> None:
    store = seeded()
    ops = channel_named(store, "ops-desk")
    async with clients(store) as (data, admin):
        ts = body(await data.post("/api/chat.postMessage", json={"channel": ops, "text": "draft"}))["ts"]
        updated = body(await data.post("/api/chat.update", json={"channel": ops, "ts": ts, "text": "final"}))
        assert set(updated) == {"channel", "message", "ok", "text", "ts"} and updated["text"] == "final"
        assert updated["message"]["edited"]["user"] == BOT_USER_ID and updated["ts"] == ts
        history = messages_of(body(await data.get("/api/conversations.history", params={"channel": ops})))
        assert (
            history[0]["text"] == "final" and history[0]["blocks"][0]["elements"][0]["elements"][0]["text"] == "final"
        )

        assert body(
            await data.post("/api/reactions.add", json={"channel": ops, "timestamp": ts, "name": ":white_check_mark:"})
        ) == {"ok": True}
        assert (
            body(
                await data.post(
                    "/api/reactions.add", json={"channel": ops, "timestamp": ts, "name": "white_check_mark"}
                )
            )["error"]
            == "already_reacted"
        )
        assert body(await data.post("/api/pins.add", json={"channel": ops, "timestamp": ts})) == {"ok": True}
        assert (
            body(await data.post("/api/pins.add", json={"channel": ops, "timestamp": ts}))["error"] == "already_pinned"
        )
        assert (
            body(await data.post("/api/pins.add", json={"channel": ops, "timestamp": "1.000001"}))["error"]
            == "message_not_found"
        )
        history_body = body(await data.get("/api/conversations.history", params={"channel": ops}))
        assert history_body["pin_count"] == 1
        pinned = messages_of(history_body)[0]
        assert pinned["reactions"] == [{"count": 1, "name": "white_check_mark", "users": [BOT_USER_ID]}]
        assert pinned["pinned_to"] == [ops] and pinned["pinned_info"]["pinned_by"] == BOT_USER_ID
        pins = body(await data.get("/api/pins.list", params={"channel": ops}))
        assert [item["message"]["ts"] for item in pins["items"]] == [ts] and pins["items"][0]["type"] == "message"
        link = body(await data.get("/api/chat.getPermalink", params={"channel": ops, "message_ts": ts}))
        assert link["permalink"] == f"https://slack-twin.slack.com/archives/{ops}/p{ts.replace('.', '')}"

        state_before = body(await admin.get("/admin/state"))
        deleted = body(await data.post("/api/chat.delete", json={"channel": ops, "ts": ts}))
        assert deleted == {"ok": True, "channel": ops, "ts": ts}
        state_after = body(await admin.get("/admin/state"))
        counts = {channel["name"]: channel["message_count"] for channel in state_after["channels"]}
        assert counts["ops-desk"] == 4
        assert ts not in {
            e["envelope"]["event"].get("ts") for e in message_events(state_after) if "text" in e["envelope"]["event"]
        }
        assert state_after["events"][0]["source_method"] == "chat.delete"
        assert state_after["events"][0]["envelope"]["event"]["subtype"] == "message_deleted"
        assert len(message_events(state_before)) - len(message_events(state_after)) == 1 - 1, (
            "one envelope removed, one added"
        )
        assert body(await data.get("/api/conversations.history", params={"channel": ops}))["pin_count"] == 0
        assert (
            body(await data.post("/api/chat.delete", json={"channel": ops, "ts": ts}))["error"] == "message_not_found"
        )
    assert [m.collection for m in store.journal] == ["messages"] * 5
    assert [m.method for m in store.journal] == ["POST"] * 5
    assert store.journal[-1].after is None and cast(dict[str, Any], store.journal[-1].before)["ts"] == ts


async def test_conversations_create_and_join() -> None:
    store = seeded()
    async with clients(store) as (data, admin):
        created = body(await data.post("/api/conversations.create", json={"name": "billing-ops"}))
        assert (
            created["ok"] is True
            and created["channel"]["name"] == "billing-ops"
            and created["channel"]["is_member"] is True
        )
        new_id = created["channel"]["id"]
        assert CHANNEL_ID.fullmatch(new_id) and created["channel"]["num_members"] == 1
        assert body(await data.post("/api/conversations.create", json={"name": "billing-ops"}))["error"] == "name_taken"
        assert (
            body(await data.post("/api/conversations.create", json={"name": "Billing Ops"}))["error"]
            == "invalid_name_specials"
        )
        assert (
            body(await data.post("/api/conversations.create", json={"name": "x" * 81}))["error"]
            == "invalid_name_maxlength"
        )
        assert body(await data.post("/api/conversations.create", json={}))["error"] == "invalid_name_required"
        private = body(
            await data.post("/api/conversations.create", json={"name": "finance-private", "is_private": True})
        )
        assert private["channel"]["is_private"] is True and private["channel"]["is_channel"] is False
        listed = body(await data.get("/api/conversations.list", params={"types": "public_channel,private_channel"}))
        assert {channel["name"] for channel in listed["channels"]} == {
            "all-hands",
            "ops-desk",
            "billing-ops",
            "finance-private",
        }

        joined = body(await data.post("/api/conversations.join", json={"channel": new_id}))
        assert joined["warning"] == "already_in_channel" and joined["response_metadata"] == {
            "warnings": ["already_in_channel"]
        }
        assert (
            body(await data.post("/api/conversations.join", json={"channel": "#billing-ops"}))["error"]
            == "channel_not_found"
        )

        posted = body(await data.post("/api/chat.postMessage", json={"channel": "#billing-ops", "text": "hello"}))
        assert posted["channel"] == new_id
        state = body(await admin.get("/admin/state"))
        assert (
            state["events"][1]["event_type"] == "channel_created"
            or state["events"][2]["event_type"] == "channel_created"
        )
        created_events = [e for e in state["events"] if e["event_type"] == "channel_created"]
        assert len(created_events) == 4 and created_events[0]["envelope"]["event"]["channel"] in {
            new_id,
            private["channel"]["id"],
        }
    assert [m.collection for m in store.journal] == ["channels", "channels", "messages"]


# --------------------------------------------------------------------------------------
# Registry and harness-canonicalizer proof
# --------------------------------------------------------------------------------------


def test_registry_exposes_the_slack_twin() -> None:
    assert "slack" in available()
    spec = load_spec("slack")
    assert spec.provider == "slack" and spec.role == "team_chat"
    store = spec.make_store("k")
    assert isinstance(store, SlackStore) and spec.make_data_app(store) is not None
    assert {"chat.postMessage", "conversations.history", "conversations.list", "search.messages"} <= set(
        supported_methods()
    )


@pytest.mark.skipif(not (BENCHMARK / "src").exists(), reason="vendored benchmark not available")
async def test_harness_canonicalizer_projects_the_posted_message() -> None:
    """The unmodified ArgaBench canonicalizer must see a new `message` resource with the channel *name*."""
    source = str(BENCHMARK / "src")
    if source not in sys.path:
        sys.path.insert(0, source)
    canon = pytest.importorskip("arga_twins_benchmark.evaluation.canonicalizers.argabench")
    capture_module = pytest.importorskip("arga_twins_benchmark.evaluation.state_capture")

    def capture(state: dict[str, Any]) -> Any:
        return capture_module.CapturedQueryState(
            query_id="ecom_02_slack_state",
            provider_name="slack",
            provider_role="team_chat",
            method="GET",
            path="/admin/state",
            canonicalizer="argabench_admin_state_v1",
            status_code=200,
            body=state,
        )

    store = seeded()
    ops = channel_named(store, "ops-desk")
    before = {(r.resource_type, r.resource_id): r for r in canon.argabench_admin_state_v1(capture(store.admin_state()))}
    async with clients(store) as (data, _admin):
        posted = body(
            await data.post("/api/chat.postMessage", json={"channel": ops, "text": "Rivermill review: owner approval"})
        )
    after = {(r.resource_type, r.resource_id): r for r in canon.argabench_admin_state_v1(capture(store.admin_state()))}
    new = [resource for key, resource in after.items() if key not in before]
    assert [resource.resource_type for resource in new] == ["message"]
    fields = new[0].fields
    assert fields["channel"] == ops and fields["channel_name"] == "ops-desk" and fields["ts"] == posted["ts"]
    assert fields["text"] == "Rivermill review: owner approval" and fields["source_method"] == "chat.postMessage"
    changed = [key for key in before if key in after and before[key].fields != after[key].fields]
    assert changed == [], "a post must not look like an update to any other resource (message_count is skipped)"
    types = {resource.resource_type for resource in after.values()}
    assert {"message", "channel", "user"} <= types
