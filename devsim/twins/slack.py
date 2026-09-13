"""Slack twin: a deterministic, grader-faithful stand-in for the Arga Slack twin.

Calibration (see devsim/calibration/slack/NOTES.md). The benchmark's own recorded twin traffic in
`tests/fixtures/argabench_crm_legacy/historical-fable-5-high-crm.tar.gz` (CRM-01..08) fixes:

- the `/admin/state` shape: 18 top-level keys; **messages live in `events[]` as Events-API
  envelopes** (`envelope.event = {channel, channel_type, text, ts, type, user}`), channels carry a
  `message_count`, and there is no `channels[].messages`. The harness canonicalizer
  (`_slack_event_messages`) reads precisely that layout and joins `channels[].name` onto it;
- the response shapes and ordering of `conversations.list`, `conversations.history` (newest
  first) and `chat.postMessage` (echoes the full bot message, `bot_profile` included);
- the twin's identity constants (team `TTWIN0001`, app `ATWIN0001`, bot `UTWINBOT` / `BTWINBOT01`).

Every other method follows the public Slack Web API and is listed as uncalibrated in NOTES.md.
Design rules from docs/TRACK-DEVSIM.md §2 apply throughout: deterministic ids and timestamps,
pure reads, real error envelopes (HTTP 200 + `{"ok": false, "error": …}`), and unsafe writes that
work so a careless agent is caught by the grader instead of a 404.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from devsim.twins.base import (
    Clock,
    Store,
    TwinSpec,
    det_alnum,
    det_hex,
    det_uuid,
    json_response,
    parse_body,
    query_params,
)

JsonDict = dict[str, Any]

# --------------------------------------------------------------------------------------
# Identity constants (byte-identical to the Arga Slack twin in the CRM fixture)
# --------------------------------------------------------------------------------------

TEAM_ID = "TTWIN0001"
TEAM_NAME = "Default Workspace"
TEAM_DOMAIN = "slack-twin"
TEAM_URL = "https://slack-twin.slack.com/"
APP_ID = "ATWIN0001"
BOT_USER_ID = "UTWINBOT"
BOT_ID = "BTWINBOT01"
BOT_USER_NAME = "slack-twin-bot"
BOT_REAL_NAME = "Slack Twin Bot"
HUMAN_USER_ID = "UTWINUSR"
HUMAN_USER_NAME = "slack-twin-user"
HUMAN_REAL_NAME = "Slack Twin User"
EVENT_TOKEN = "slack-twin"
NEVER_READ = "0000000000.000000"

ADMIN_STATE_KEYS: tuple[str, ...] = (
    "apps",
    "base_time",
    "canvases",
    "channels",
    "custom_emoji",
    "deliveries",
    "events",
    "failure_rules",
    "files",
    "legacy_preferences",
    "legacy_resources",
    "logical_now",
    "rate_limiting_enabled",
    "seed",
    "subscriptions",
    "team",
    "triggers",
    "users",
)

_BOT_SCOPES = (
    "channels:history",
    "channels:manage",
    "channels:read",
    "chat:write",
    "emoji:read",
    "files:read",
    "files:write",
    "im:history",
    "im:read",
    "im:write",
    "mpim:history",
    "mpim:read",
    "mpim:write",
    "reactions:write",
    "search:read",
    "team:read",
    "users:read",
)
_USER_SCOPES = (
    "search:read",
    "channels:read",
    "channels:history",
    "emoji:read",
    "groups:history",
    "im:history",
    "im:read",
    "im:write",
    "mpim:history",
    "mpim:read",
    "mpim:write",
)

_CHANNEL_NAME = re.compile(r"^[a-z0-9_-]+$")
_MAX_CHANNEL_NAME = 80
_MAX_LIMIT = 1000


# --------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------


def _clone(value: object) -> Any:
    """Deep copy with sorted keys, so responses and admin state render like the twin's."""
    return json.loads(json.dumps(value, sort_keys=True, default=str))


def _mapping_list(value: object) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [cast(Mapping[str, Any], item) for item in cast(list[object], value) if isinstance(item, Mapping)]


def _ts_key(value: str) -> tuple[int, int]:
    seconds, _, fraction = value.partition(".")
    try:
        return int(seconds or "0"), int((fraction + "000000")[:6])
    except ValueError:
        return (0, 0)


def _title(name: str) -> str:
    return " ".join(part.capitalize() for part in re.split(r"[-_.\s]+", name) if part) or name


def _rich_text_blocks(text: str) -> list[JsonDict]:
    return [
        {
            "block_id": "twin",
            "elements": [{"elements": [{"text": text, "type": "text"}], "type": "rich_text_section"}],
            "type": "rich_text",
        }
    ]


def _bot_profile() -> JsonDict:
    return {
        "app_id": APP_ID,
        "deleted": False,
        "icons": {f"image_{size}": f"https://avatars.slack-twin.local/{BOT_ID}_{size}.png" for size in (36, 48, 72)},
        "id": BOT_ID,
        "name": BOT_REAL_NAME,
        "team_id": TEAM_ID,
        "updated": 0,
        "user_id": BOT_USER_ID,
    }


def _user_record(user_id: str, name: str, real_name: str, *, is_bot: bool) -> JsonDict:
    first, _, last = real_name.partition(" ")
    return {
        "color": "4a154b",
        "deleted": False,
        "id": user_id,
        "is_admin": False,
        "is_app_user": is_bot,
        "is_bot": is_bot,
        "is_email_confirmed": True,
        "is_owner": False,
        "is_primary_owner": False,
        "is_restricted": False,
        "is_ultra_restricted": False,
        "name": name,
        "profile": {
            "always_active": False,
            "api_app_id": "",
            "avatar_hash": "",
            "bot_id": "",
            "display_name": name,
            "display_name_normalized": name,
            "email": f"{name}@{TEAM_DOMAIN}.local",
            "fields": {},
            "first_name": first,
            "huddle_state": "default_unset",
            "huddle_state_expiration_ts": 0,
            "image_1024": "",
            "image_192": "",
            "image_24": "",
            "image_32": "",
            "image_48": "",
            "image_512": "",
            "image_72": "",
            "image_original": "",
            "is_custom_image": False,
            "last_name": last,
            "phone": "",
            "real_name": real_name,
            "real_name_normalized": real_name,
            "skype": "",
            "status_emoji": "",
            "status_emoji_display_info": [],
            "status_expiration": 0,
            "status_text": "",
            "status_text_canonical": "",
            "team": TEAM_ID,
            "title": "",
        },
        "real_name": real_name,
        "team_id": TEAM_ID,
        "tz": "UTC",
        "tz_label": "UTC",
        "tz_offset": 0,
        "updated": 0,
        "who_can_share_contact_card": "EVERYONE",
    }


def _channel_record(channel_id: str, name: str, created: int, creator: str, *, is_private: bool) -> JsonDict:
    """The 36 stored channel keys; `is_member`, `num_members` (+ admin `message_count`) are derived."""
    return {
        "connected_limited_team_ids": [],
        "connected_team_ids": [TEAM_ID],
        "context_team_id": TEAM_ID,
        "conversation_host_id": TEAM_ID,
        "created": created,
        "creator": creator,
        "id": channel_id,
        "internal_team_ids": [TEAM_ID],
        "is_archived": False,
        "is_channel": not is_private,
        "is_ext_shared": False,
        "is_general": False,
        "is_group": is_private,
        "is_im": False,
        "is_mpim": False,
        "is_open": True,
        "is_org_shared": False,
        "is_pending_ext_shared": False,
        "is_private": is_private,
        "is_read_only": False,
        "is_shared": False,
        "is_thread_only": False,
        "last_read": NEVER_READ,
        "name": name,
        "name_normalized": name,
        "parent_conversation": None,
        "pending_connected_team_ids": [],
        "pending_shared": [],
        "previous_names": [name],
        "properties": {
            "has_slack_connect_invite_created": False,
            "is_dormant": False,
            "tabs": [{"id": "files", "label": "Files", "type": "files"}],
            "tabz": [{"type": "files"}],
            "use_case": "other",
        },
        "purpose": {"creator": "", "last_set": created, "value": ""},
        "shared_team_ids": [TEAM_ID],
        "topic": {"creator": "", "last_set": 0, "value": ""},
        "unlinked": 0,
        "updated": created,
    }


def _im_record(channel_id: str, user_id: str, created: int) -> JsonDict:
    return {
        "context_team_id": TEAM_ID,
        "created": created,
        "id": channel_id,
        "is_archived": False,
        "is_im": True,
        "is_open": True,
        "is_org_shared": False,
        "is_user_deleted": False,
        "last_read": NEVER_READ,
        "priority": 0,
        "updated": created,
        "user": user_id,
    }


def _permalink(channel_id: str, ts: str) -> str:
    return f"{TEAM_URL}archives/{channel_id}/p{ts.replace('.', '')}"


def _encode_cursor(kind: str, value: str) -> str:
    return base64.b64encode(f"{kind}:{value}".encode()).decode("ascii")


def _decode_cursor(kind: str, cursor: str) -> str | None:
    try:
        decoded = base64.b64decode(cursor.encode("ascii"), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    prefix = f"{kind}:"
    return decoded[len(prefix) :] if decoded.startswith(prefix) and len(decoded) > len(prefix) else None


# --------------------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------------------


class SlackStore(Store):
    """In-memory Slack workspace: one team, users, channels (+ IMs), messages and the event log."""

    provider = "slack"

    def __init__(self, seed_key: str, clock: Clock | None = None) -> None:
        super().__init__(seed_key, clock)
        self.team: JsonDict = {"app_id": APP_ID, "domain": TEAM_DOMAIN, "id": TEAM_ID, "name": TEAM_NAME}
        self.users: dict[str, JsonDict] = {}
        self.channels: dict[str, JsonDict] = {}
        self.events: list[JsonDict] = []  # newest first, like the twin
        self._members: dict[str, set[str]] = {}
        self._messages: dict[str, dict[str, JsonDict]] = {}
        self._pins: dict[str, list[str]] = {}
        self._ims: dict[str, str] = {}
        self._add_user(BOT_USER_ID, BOT_USER_NAME, BOT_REAL_NAME, is_bot=True)
        self._add_user(HUMAN_USER_ID, HUMAN_USER_NAME, HUMAN_REAL_NAME, is_bot=False)

    # -- seeding -----------------------------------------------------------------------

    def seed(self, seed_config: Mapping[str, Any]) -> None:
        """Load `seed_config["slack"]` (or the slice itself): users, then channels with messages in order.

        The real twin seeds through its own API, so every seeded channel leaves a `channel_created`
        event and every seeded message a `message` event, exactly like the fixture.
        """
        nested = seed_config.get("slack")
        config: Mapping[str, Any] = cast(Mapping[str, Any], nested) if isinstance(nested, Mapping) else seed_config
        for raw_user in _mapping_list(config.get("users")):
            name = raw_user.get("name")
            if isinstance(name, str) and name:
                real_name = raw_user.get("real_name")
                self.ensure_user(name, real_name if isinstance(real_name, str) else None)
        for raw_channel in _mapping_list(config.get("channels")):
            name = raw_channel.get("name")
            if not isinstance(name, str) or not name:
                continue
            channel_id = self.create_channel(name.lstrip("#"), is_private=bool(raw_channel.get("is_private", False)))
            for field in ("topic", "purpose"):
                value = raw_channel.get(field)
                if isinstance(value, str) and value:
                    self.channels[channel_id][field] = {
                        "creator": BOT_USER_ID,
                        "last_set": self.clock.epoch(),
                        "value": value,
                    }
            for raw_message in _mapping_list(raw_channel.get("messages")):
                text = raw_message.get("text")
                if not isinstance(text, str):
                    continue
                author_name = raw_message.get("user")
                author = self.ensure_user(
                    author_name if isinstance(author_name, str) and author_name else HUMAN_USER_NAME
                )
                self._members[channel_id].add(author)
                self.post_message(channel_id, text, user=author)

    # -- users -------------------------------------------------------------------------

    def _add_user(self, user_id: str, name: str, real_name: str, *, is_bot: bool) -> str:
        self.users[user_id] = _user_record(user_id, name, real_name, is_bot=is_bot)
        return user_id

    def user_by_name(self, name: str) -> str | None:
        handle = name.lstrip("@")
        for user_id, record in self.users.items():
            if record["name"] == handle:
                return user_id
        return None

    def ensure_user(self, name: str, real_name: str | None = None) -> str:
        existing = self.user_by_name(name)
        if existing is not None:
            return existing
        return self._add_user(self._new_user_id(name), name, real_name or _title(name), is_bot=False)

    def _new_user_id(self, name: str) -> str:
        """`U` + up to five letters of the handle + deterministic filler: `UAUDIT25BD`-style, 10 chars."""
        stem = re.sub(r"[^A-Za-z0-9]", "", name).upper()[:5] or "USER"
        ordinal = self.next_ordinal("users")
        for attempt in range(1_000):
            suffix = det_alnum(self.seed_key, "users", ordinal, attempt, length=9 - len(stem), case="upper")
            candidate = f"U{stem}{suffix}"
            if candidate not in self.users:
                return candidate
        raise RuntimeError("exhausted user id space")

    # -- channels ----------------------------------------------------------------------

    def _new_channel_id(self, prefix: str, collection: str) -> str:
        ordinal = self.next_ordinal(collection)
        for attempt in range(1_000):
            candidate = prefix + det_alnum(self.seed_key, collection, ordinal, attempt, length=10, case="upper")
            if candidate not in self.channels:
                return candidate
        raise RuntimeError("exhausted channel id space")

    def channel_by_name(self, name: str) -> str | None:
        wanted = name.lstrip("#")
        for channel_id, record in self.channels.items():
            if record.get("name") == wanted:
                return channel_id
        return None

    def resolve_channel(self, reference: str | None, *, allow_names: bool) -> str | None:
        """Ids always resolve; `#name` / `name` only where Slack itself accepts them (chat.postMessage)."""
        if not reference:
            return None
        if reference in self.channels:
            return reference
        return self.channel_by_name(reference) if allow_names else None

    def create_channel(self, name: str, *, is_private: bool = False, creator: str = BOT_USER_ID) -> str:
        channel_id = self._new_channel_id("C", "channels")
        created = self.clock.epoch()
        self.channels[channel_id] = _channel_record(channel_id, name, created, creator, is_private=is_private)
        self._members[channel_id] = {creator}
        self._messages[channel_id] = {}
        self._pins[channel_id] = []
        self._emit_event(
            {"channel": channel_id, "created": created, "type": "channel_created"},
            event_type="channel_created",
            source_method="conversations.create",
            user=creator,
        )
        return channel_id

    def open_im(self, user_id: str, *, source_method: str = "conversations.open") -> tuple[str, bool]:
        """Return `(im_channel_id, created)`; idempotent per user, like `conversations.open`."""
        existing = self._ims.get(user_id)
        if existing is not None:
            return existing, False
        channel_id = self._new_channel_id("D", "ims")
        created = self.clock.epoch()
        self.channels[channel_id] = _im_record(channel_id, user_id, created)
        self._members[channel_id] = {BOT_USER_ID, user_id}
        self._messages[channel_id] = {}
        self._pins[channel_id] = []
        self._ims[user_id] = channel_id
        self._emit_event(
            {"channel": channel_id, "type": "im_created", "user": user_id},
            event_type="im_created",
            source_method=source_method,
            user=BOT_USER_ID,
        )
        return channel_id, True

    def is_member(self, channel_id: str, user_id: str) -> bool:
        return user_id in self._members.get(channel_id, set())

    def members(self, channel_id: str) -> list[str]:
        return sorted(self._members.get(channel_id, set()))

    def join_channel(self, channel_id: str, user_id: str) -> bool:
        if self.is_member(channel_id, user_id):
            return False
        self._members[channel_id].add(user_id)
        self._emit_event(
            {
                "channel": channel_id,
                "channel_type": self._channel_type(channel_id),
                "inviter": user_id,
                "team": TEAM_ID,
                "type": "member_joined_channel",
                "user": user_id,
            },
            event_type="member_joined_channel",
            source_method="conversations.join",
            user=user_id,
        )
        return True

    def _channel_type(self, channel_id: str) -> str:
        record = self.channels[channel_id]
        if record.get("is_im"):
            return "im"
        return "group" if record.get("is_private") else "channel"

    def matches_types(self, channel_id: str, types: set[str]) -> bool:
        record = self.channels[channel_id]
        if record.get("is_im"):
            return "im" in types
        if record.get("is_mpim"):
            return "mpim" in types
        return "private_channel" in types if record.get("is_private") else "public_channel" in types

    def channel_view(self, channel_id: str, *, admin: bool = False) -> JsonDict:
        """The API/admin projection of a conversation. `message_count` is admin-only (fixture)."""
        record = self.channels[channel_id]
        view = cast(JsonDict, _clone(record))
        if not record.get("is_im"):
            view["is_member"] = self.is_member(channel_id, BOT_USER_ID)
            view["num_members"] = len(self._members.get(channel_id, set()))
        if admin:
            view["message_count"] = len(self._messages.get(channel_id, {}))
        return view

    # -- messages ----------------------------------------------------------------------

    def find_message(self, channel_id: str, ts: str | None) -> JsonDict | None:
        if ts is None:
            return None
        return self._messages.get(channel_id, {}).get(ts)

    def channel_messages(self, channel_id: str) -> list[JsonDict]:
        """Top-level messages (thread parents included, replies excluded), newest first."""
        messages = [
            message
            for message in self._messages.get(channel_id, {}).values()
            if message.get("thread_ts") in (None, message["ts"])
        ]
        return sorted(messages, key=lambda message: _ts_key(str(message["ts"])), reverse=True)

    def thread_messages(self, channel_id: str, thread_ts: str) -> list[JsonDict]:
        """Parent first, then replies oldest to newest (Slack `conversations.replies` order)."""
        parent = self.find_message(channel_id, thread_ts)
        if parent is None:
            return []
        replies = [
            message
            for message in self._messages[channel_id].values()
            if message.get("thread_ts") == thread_ts and message["ts"] != thread_ts
        ]
        return [parent, *sorted(replies, key=lambda message: _ts_key(str(message["ts"])))]

    def post_message(
        self,
        channel_id: str,
        text: str,
        *,
        user: str,
        thread_ts: str | None = None,
        blocks: list[Any] | None = None,
        attachments: list[Any] | None = None,
        username: str | None = None,
        icon_emoji: str | None = None,
        icon_url: str | None = None,
        source_method: str = "chat.postMessage",
    ) -> JsonDict:
        ts = self.clock.slack_ts(self.next_ordinal("messages"))
        message: JsonDict = {
            "blocks": blocks if blocks is not None else _rich_text_blocks(text),
            "client_msg_id": det_hex(self.seed_key, "client_msg_id", channel_id, ts, length=40),
            "team": TEAM_ID,
            "text": text,
            "ts": ts,
            "type": "message",
            "user": user,
        }
        if user == BOT_USER_ID:
            message.update({"app_id": APP_ID, "bot_id": BOT_ID, "bot_profile": _bot_profile()})
        if attachments is not None:
            message["attachments"] = attachments
        if username is not None:
            icons: JsonDict = {}
            if icon_emoji is not None:
                icons["emoji"] = icon_emoji
            if icon_url is not None:
                icons["image_64"] = icon_url
            message.update({"subtype": "bot_message", "username": username, "icons": icons})
        event: JsonDict = {
            "channel": channel_id,
            "channel_type": self._channel_type(channel_id),
            "text": text,
            "ts": ts,
            "type": "message",
            "user": user,
        }
        if thread_ts is not None:
            parent = self._messages[channel_id][thread_ts]
            root_ts = str(parent.get("thread_ts") or parent["ts"])
            root = self._messages[channel_id][root_ts]
            message["thread_ts"] = root_ts
            message["parent_user_id"] = root["user"]
            event["thread_ts"] = root_ts
            self._messages[channel_id][ts] = message
            self._refresh_thread(root, channel_id)
        else:
            self._messages[channel_id][ts] = message
        self._emit_event(event, event_type="message", source_method=source_method, user=user)
        return message

    def _refresh_thread(self, root: JsonDict, channel_id: str) -> None:
        replies = self.thread_messages(channel_id, str(root["ts"]))[1:]
        reply_users = list(dict.fromkeys(str(reply["user"]) for reply in replies))
        root.update(
            {
                "is_locked": False,
                "latest_reply": replies[-1]["ts"] if replies else root["ts"],
                "reply_count": len(replies),
                "reply_users": reply_users,
                "reply_users_count": len(reply_users),
                "subscribed": False,
                "thread_ts": root["ts"],
            }
        )

    def update_message(self, channel_id: str, ts: str, *, text: str | None, blocks: list[Any] | None) -> JsonDict:
        message = self._messages[channel_id][ts]
        previous = cast(JsonDict, _clone(message))
        if text is not None:
            message["text"] = text
        if blocks is not None:
            message["blocks"] = blocks
        elif text is not None:
            message["blocks"] = _rich_text_blocks(text)
        edit_ts = self.clock.slack_ts(self.next_ordinal("messages"))
        message["edited"] = {"ts": edit_ts, "user": BOT_USER_ID}
        self._emit_event(
            {
                "channel": channel_id,
                "channel_type": self._channel_type(channel_id),
                "event_ts": edit_ts,
                "hidden": True,
                "message": _clone(message),
                "previous_message": previous,
                "subtype": "message_changed",
                "ts": edit_ts,
                "type": "message",
            },
            event_type="message",
            source_method="chat.update",
            user=BOT_USER_ID,
        )
        return message

    def delete_message(self, channel_id: str, ts: str) -> JsonDict:
        """Remove the message and its original `message` event so state-diff graders see a deletion."""
        removed = self._messages[channel_id].pop(ts)
        if ts in self._pins.get(channel_id, []):
            self._pins[channel_id].remove(ts)
        self.events = [
            event
            for event in self.events
            if not (
                event["event_type"] == "message"
                and event["envelope"]["event"].get("channel") == channel_id
                and event["envelope"]["event"].get("ts") == ts
            )
        ]
        parent_ts = removed.get("thread_ts")
        if isinstance(parent_ts, str) and parent_ts != ts and parent_ts in self._messages[channel_id]:
            self._refresh_thread(self._messages[channel_id][parent_ts], channel_id)
        delete_ts = self.clock.slack_ts(self.next_ordinal("messages"))
        self._emit_event(
            {
                "channel": channel_id,
                "channel_type": self._channel_type(channel_id),
                "deleted_ts": ts,
                "event_ts": delete_ts,
                "hidden": True,
                "previous_message": removed,
                "subtype": "message_deleted",
                "ts": delete_ts,
                "type": "message",
            },
            event_type="message",
            source_method="chat.delete",
            user=BOT_USER_ID,
        )
        return removed

    def add_reaction(self, channel_id: str, ts: str, name: str, *, user: str) -> bool:
        message = self._messages[channel_id][ts]
        reactions = cast(list[JsonDict], message.setdefault("reactions", []))
        for reaction in reactions:
            if reaction["name"] == name:
                if user in reaction["users"]:
                    return False
                reaction["users"].append(user)
                reaction["count"] = len(reaction["users"])
                break
        else:
            reactions.append({"count": 1, "name": name, "users": [user]})
        self._emit_event(
            {
                "event_ts": self.clock.slack_ts(self.next_ordinal("messages")),
                "item": {"channel": channel_id, "ts": ts, "type": "message"},
                "item_user": message["user"],
                "reaction": name,
                "type": "reaction_added",
                "user": user,
            },
            event_type="reaction_added",
            source_method="reactions.add",
            user=user,
        )
        return True

    def pin_message(self, channel_id: str, ts: str, *, user: str) -> bool:
        if ts in self._pins[channel_id]:
            return False
        message = self._messages[channel_id][ts]
        pinned_ts = self.clock.epoch()
        message["pinned_to"] = [channel_id]
        message["pinned_info"] = {"channel": channel_id, "pinned_by": user, "pinned_ts": pinned_ts}
        self._pins[channel_id].append(ts)
        self._emit_event(
            {
                "channel_id": channel_id,
                "event_ts": self.clock.slack_ts(self.next_ordinal("messages")),
                "item": {"channel": channel_id, "created": pinned_ts, "created_by": user, "message": _clone(message)},
                "type": "pin_added",
                "user": user,
            },
            event_type="pin_added",
            source_method="pins.add",
            user=user,
        )
        return True

    def pins(self, channel_id: str) -> list[JsonDict]:
        return [
            self._messages[channel_id][ts] for ts in self._pins.get(channel_id, []) if ts in self._messages[channel_id]
        ]

    def search_messages(self, query: str) -> list[tuple[str, JsonDict]]:
        """Case-insensitive substring AND-match over text, honouring `in:` and `from:` modifiers."""
        terms: list[str] = []
        channel_filter: str | None = None
        user_filter: str | None = None
        for token in re.findall(r'"[^"]*"|\S+', query):
            lowered = token.casefold()
            if lowered.startswith("in:"):
                channel_filter = token[3:].lstrip("#")
            elif lowered.startswith("from:"):
                user_filter = token[5:].lstrip("@")
            else:
                terms.append(token.strip('"').casefold())
        results: list[tuple[str, JsonDict]] = []
        for channel_id, messages in self._messages.items():
            record = self.channels[channel_id]
            if channel_filter is not None and channel_filter not in (channel_id, record.get("name")):
                continue
            for message in messages.values():
                author = str(message.get("user", ""))
                author_name = str(self.users.get(author, {}).get("name", ""))
                if user_filter is not None and user_filter not in (author, author_name):
                    continue
                text = str(message.get("text", "")).casefold()
                if all(term in text for term in terms):
                    results.append((channel_id, message))
        return sorted(results, key=lambda pair: _ts_key(str(pair[1]["ts"])), reverse=True)

    # -- events ------------------------------------------------------------------------

    def _emit_event(self, event: JsonDict, *, event_type: str, source_method: str, user: str) -> JsonDict:
        ordinal = self.next_ordinal("events")
        event_id = "Ev" + det_hex(self.seed_key, "events", ordinal, length=11).upper()
        record: JsonDict = {
            "created_at": self.clock.now().replace(microsecond=ordinal % 1_000_000).isoformat(),
            "envelope": {
                "api_app_id": APP_ID,
                "authed_users": [user],
                "authorizations": [
                    {
                        "enterprise_id": None,
                        "is_bot": user == BOT_USER_ID,
                        "is_enterprise_install": False,
                        "team_id": TEAM_ID,
                        "user_id": user,
                    }
                ],
                "event": event,
                "event_id": event_id,
                "event_time": self.clock.epoch(),
                "team_id": TEAM_ID,
                "token": EVENT_TOKEN,
                "type": "event_callback",
            },
            "event_type": event_type,
            "id": event_id,
            "pending": True,
            "source_method": source_method,
        }
        self.events.insert(0, record)
        return record

    # -- admin plane -------------------------------------------------------------------

    def app_record(self) -> JsonDict:
        return {
            "app_id": APP_ID,
            "hosted_variable_names": [],
            "installed_team_ids": [TEAM_ID],
            "manifest": {
                "display_information": {"name": TEAM_NAME},
                "features": {"bot_user": {"always_online": True, "display_name": BOT_USER_NAME}},
                "oauth_config": {"scopes": {"bot": list(_BOT_SCOPES), "user": list(_USER_SCOPES)}},
                "settings": {"org_deploy_enabled": False, "socket_mode_enabled": True},
            },
        }

    def admin_state(self) -> dict[str, Any]:
        """The 18-key twin shape. Pure: derived from the store, never mutating it."""
        state: JsonDict = {
            "apps": [self.app_record()],
            "base_time": None,
            "canvases": [],
            "channels": [self.channel_view(channel_id, admin=True) for channel_id in sorted(self.channels)],
            "custom_emoji": {},
            "deliveries": [],
            "events": list(self.events),
            "failure_rules": [],
            "files": [],
            "legacy_preferences": {},
            "legacy_resources": {},
            "logical_now": self.clock.now().isoformat(),
            "rate_limiting_enabled": False,
            "seed": 1,
            "subscriptions": [],
            "team": dict(self.team),
            "triggers": [],
            "users": [self.users[user_id] for user_id in sorted(self.users)],
        }
        return cast(dict[str, Any], _clone(state))


# --------------------------------------------------------------------------------------
# Parameter access (JSON bodies keep types, form/query bodies are strings)
# --------------------------------------------------------------------------------------


def _string(params: Mapping[str, Any], key: str) -> str | None:
    value = params.get(key)
    if isinstance(value, str):
        return value
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return str(value)
    return None


def _int(params: Mapping[str, Any], key: str, default: int) -> int | None:
    """`None` means the value was present but not an integer."""
    value = params.get(key)
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        return int(value)
    return None


def _bool(params: Mapping[str, Any], key: str, default: bool) -> bool:
    value = params.get(key)
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return bool(value)
    return str(value).strip().casefold() in {"1", "true", "yes", "on"}


def _json_list(params: Mapping[str, Any], key: str) -> list[Any] | None:
    value = params.get(key)
    if isinstance(value, str) and value.strip():
        try:
            parsed: object = json.loads(value)
        except json.JSONDecodeError:
            return None
        value = parsed
    return cast(list[Any], value) if isinstance(value, list) else None


# --------------------------------------------------------------------------------------
# Web API methods
# --------------------------------------------------------------------------------------

_HandlerFn = Callable[[SlackStore, JsonDict, str], JsonDict]


@dataclass(frozen=True)
class _Method:
    name: str
    write: bool
    handler: _HandlerFn


_METHODS: dict[str, _Method] = {}


def _api(name: str, *, write: bool = False) -> Callable[[_HandlerFn], _HandlerFn]:
    def register(handler: _HandlerFn) -> _HandlerFn:
        _METHODS[name.casefold()] = _Method(name, write, handler)
        return handler

    return register


def _error(code: str, **extra: Any) -> JsonDict:
    return {"ok": False, "error": code, **extra}


def _invalid_arguments(*missing: str) -> JsonDict:
    return _error(
        "invalid_arguments",
        response_metadata={"messages": [f"[ERROR] missing required field: {field}" for field in missing]},
    )


def _limit(params: Mapping[str, Any], default: int) -> int | JsonDict:
    value = _int(params, "limit", default)
    if value is None or value < 1 or value > _MAX_LIMIT:
        return _error("invalid_limit")
    return value


def _channel_or_error(store: SlackStore, params: Mapping[str, Any], *, allow_names: bool = False) -> str | JsonDict:
    reference = _string(params, "channel")
    if reference is None or not reference.strip():
        return _invalid_arguments("channel")
    channel_id = store.resolve_channel(reference.strip(), allow_names=allow_names)
    return channel_id if channel_id is not None else _error("channel_not_found")


def _paginate(
    items: list[str], params: Mapping[str, Any], *, kind: str, default_limit: int
) -> tuple[list[str], str] | JsonDict:
    limit = _limit(params, default_limit)
    if isinstance(limit, dict):
        return limit
    start = 0
    cursor = _string(params, "cursor")
    if cursor:
        marker = _decode_cursor(kind, cursor)
        if marker is None or marker not in items:
            return _error("invalid_cursor")
        start = items.index(marker)
    page = items[start : start + limit]
    next_cursor = _encode_cursor(kind, items[start + limit]) if start + limit < len(items) else ""
    return page, next_cursor


# -- identity / workspace --------------------------------------------------------------


@_api("api.test")
def _api_test(store: SlackStore, params: JsonDict, path: str) -> JsonDict:
    args = {key: value for key, value in params.items() if key != "token"}
    return {"ok": True, "args": args} if args else {"ok": True}


@_api("auth.test")
def _auth_test(store: SlackStore, params: JsonDict, path: str) -> JsonDict:
    return {
        "ok": True,
        "url": TEAM_URL,
        "team": TEAM_NAME,
        "user": BOT_USER_NAME,
        "team_id": TEAM_ID,
        "user_id": BOT_USER_ID,
        "bot_id": BOT_ID,
        "is_enterprise_install": False,
    }


@_api("team.info")
def _team_info(store: SlackStore, params: JsonDict, path: str) -> JsonDict:
    return {
        "ok": True,
        "team": {
            **store.team,
            "avatar_base_url": "https://avatars.slack-twin.local/",
            "email_domain": "",
            "icon": {"image_default": True},
            "is_verified": False,
        },
    }


@_api("bots.info")
def _bots_info(store: SlackStore, params: JsonDict, path: str) -> JsonDict:
    bot = _string(params, "bot")
    if bot not in (None, "", BOT_ID):
        return _error("bot_not_found")
    profile = _bot_profile()
    return {
        "ok": True,
        "bot": {
            "app_id": APP_ID,
            "deleted": False,
            "icons": profile["icons"],
            "id": BOT_ID,
            "name": BOT_REAL_NAME,
            "updated": 0,
            "user_id": BOT_USER_ID,
        },
    }


@_api("emoji.list")
def _emoji_list(store: SlackStore, params: JsonDict, path: str) -> JsonDict:
    return {"ok": True, "emoji": {}, "cache_ts": str(store.clock.epoch())}


# -- users -----------------------------------------------------------------------------


@_api("users.list")
def _users_list(store: SlackStore, params: JsonDict, path: str) -> JsonDict:
    paged = _paginate(sorted(store.users), params, kind="user", default_limit=_MAX_LIMIT)
    if isinstance(paged, dict):
        return paged
    page, next_cursor = paged
    return {
        "ok": True,
        "members": [store.users[user_id] for user_id in page],
        "cache_ts": store.clock.epoch(),
        "response_metadata": {"next_cursor": next_cursor},
    }


@_api("users.info")
def _users_info(store: SlackStore, params: JsonDict, path: str) -> JsonDict:
    user_id = _string(params, "user")
    if user_id is None or user_id not in store.users:
        return _error("user_not_found")
    return {"ok": True, "user": store.users[user_id]}


@_api("users.lookupByEmail")
def _users_lookup_by_email(store: SlackStore, params: JsonDict, path: str) -> JsonDict:
    email = _string(params, "email")
    if not email:
        return _invalid_arguments("email")
    for record in store.users.values():
        if str(record["profile"]["email"]).casefold() == email.strip().casefold():
            return {"ok": True, "user": record}
    return _error("users_not_found")


@_api("users.conversations")
def _users_conversations(store: SlackStore, params: JsonDict, path: str) -> JsonDict:
    user_id = _string(params, "user") or BOT_USER_ID
    if user_id not in store.users:
        return _error("user_not_found")
    return _list_conversations(store, params, member=user_id)


# -- conversations ---------------------------------------------------------------------


def _list_conversations(store: SlackStore, params: JsonDict, *, member: str | None) -> JsonDict:
    types = {item.strip() for item in (_string(params, "types") or "public_channel").split(",") if item.strip()}
    exclude_archived = _bool(params, "exclude_archived", False)
    matching = [
        channel_id
        for channel_id in sorted(store.channels)
        if store.matches_types(channel_id, types)
        and not (exclude_archived and store.channels[channel_id].get("is_archived"))
        and (member is None or store.is_member(channel_id, member))
    ]
    paged = _paginate(matching, params, kind="channel", default_limit=100)
    if isinstance(paged, dict):
        return paged
    page, next_cursor = paged
    return {
        "ok": True,
        "channels": [store.channel_view(channel_id) for channel_id in page],
        "response_metadata": {"next_cursor": next_cursor},
    }


@_api("conversations.list")
def _conversations_list(store: SlackStore, params: JsonDict, path: str) -> JsonDict:
    return _list_conversations(store, params, member=None)


@_api("conversations.info")
def _conversations_info(store: SlackStore, params: JsonDict, path: str) -> JsonDict:
    channel_id = _channel_or_error(store, params)
    if isinstance(channel_id, dict):
        return channel_id
    return {"ok": True, "channel": store.channel_view(channel_id)}


@_api("conversations.members")
def _conversations_members(store: SlackStore, params: JsonDict, path: str) -> JsonDict:
    channel_id = _channel_or_error(store, params)
    if isinstance(channel_id, dict):
        return channel_id
    paged = _paginate(store.members(channel_id), params, kind="user", default_limit=100)
    if isinstance(paged, dict):
        return paged
    page, next_cursor = paged
    return {"ok": True, "members": page, "response_metadata": {"next_cursor": next_cursor}}


@_api("conversations.history")
def _conversations_history(store: SlackStore, params: JsonDict, path: str) -> JsonDict:
    channel_id = _channel_or_error(store, params)
    if isinstance(channel_id, dict):
        return channel_id
    limit = _limit(params, 100)
    if isinstance(limit, dict):
        return limit
    oldest, latest = _string(params, "oldest"), _string(params, "latest")
    inclusive = _bool(params, "inclusive", False)
    lower = _ts_key(oldest) if oldest else None
    upper = _ts_key(latest) if latest else None
    cursor = _string(params, "cursor")
    before: tuple[int, int] | None = None
    if cursor:
        marker = _decode_cursor("next_ts", cursor)
        if marker is None:
            return _error("invalid_cursor")
        before = _ts_key(marker)

    def keep(message: JsonDict) -> bool:
        key = _ts_key(str(message["ts"]))
        if before is not None and key >= before:
            return False
        if lower is not None and (key < lower or (key == lower and not inclusive)):
            return False
        return not (upper is not None and (key > upper or (key == upper and not inclusive)))

    messages = [message for message in store.channel_messages(channel_id) if keep(message)]
    page = messages[:limit]
    has_more = len(messages) > limit
    return {
        "ok": True,
        "messages": page,
        "has_more": has_more,
        "pin_count": len(store.pins(channel_id)),
        "channel_actions_ts": None,
        "channel_actions_count": 0,
        "response_metadata": {"next_cursor": _encode_cursor("next_ts", str(page[-1]["ts"])) if has_more else ""},
    }


@_api("conversations.replies")
def _conversations_replies(store: SlackStore, params: JsonDict, path: str) -> JsonDict:
    channel_id = _channel_or_error(store, params)
    if isinstance(channel_id, dict):
        return channel_id
    ts = _string(params, "ts")
    if not ts:
        return _invalid_arguments("ts")
    limit = _limit(params, 1000)
    if isinstance(limit, dict):
        return limit
    messages = store.thread_messages(channel_id, ts)
    if not messages:
        return _error("thread_not_found")
    return {
        "ok": True,
        "messages": messages[:limit],
        "has_more": len(messages) > limit,
        "response_metadata": {"next_cursor": ""},
    }


@_api("conversations.join", write=True)
def _conversations_join(store: SlackStore, params: JsonDict, path: str) -> JsonDict:
    channel_id = _channel_or_error(store, params)
    if isinstance(channel_id, dict):
        return channel_id
    if store.channels[channel_id].get("is_im"):
        return _error("method_not_supported_for_channel_type")
    before = store.channel_view(channel_id, admin=True)
    if not store.join_channel(channel_id, BOT_USER_ID):
        return {
            "ok": True,
            "channel": store.channel_view(channel_id),
            "warning": "already_in_channel",
            "response_metadata": {"warnings": ["already_in_channel"]},
        }
    after = store.channel_view(channel_id, admin=True)
    store.record_mutation(
        method="POST", path=path, collection="channels", record_id=channel_id, before=before, after=after
    )
    return {"ok": True, "channel": store.channel_view(channel_id)}


@_api("conversations.create", write=True)
def _conversations_create(store: SlackStore, params: JsonDict, path: str) -> JsonDict:
    name = _string(params, "name")
    if name is None or not name.strip():
        return _error("invalid_name_required")
    name = name.strip().lstrip("#")
    if len(name) > _MAX_CHANNEL_NAME:
        return _error("invalid_name_maxlength")
    if not _CHANNEL_NAME.fullmatch(name):
        return _error("invalid_name_specials")
    if store.channel_by_name(name) is not None:
        return _error("name_taken")
    channel_id = store.create_channel(name, is_private=_bool(params, "is_private", False))
    after = store.channel_view(channel_id, admin=True)
    store.record_mutation(
        method="POST", path=path, collection="channels", record_id=channel_id, before=None, after=after
    )
    return {"ok": True, "channel": store.channel_view(channel_id)}


@_api("conversations.open", write=True)
def _conversations_open(store: SlackStore, params: JsonDict, path: str) -> JsonDict:
    channel = _string(params, "channel")
    if channel:
        if channel not in store.channels or not store.channels[channel].get("is_im"):
            return _error("channel_not_found")
        return {"ok": True, "no_op": True, "already_open": True, "channel": {"id": channel}}
    users = [item.strip() for item in (_string(params, "users") or "").split(",") if item.strip()]
    if not users:
        return _invalid_arguments("users")
    if len(users) != 1:
        return _error("method_not_supported_for_channel_type")
    user_id = users[0]
    if user_id not in store.users:
        return _error("user_not_found")
    channel_id, created = store.open_im(user_id)
    if created:
        after = store.channel_view(channel_id, admin=True)
        store.record_mutation(
            method="POST", path=path, collection="channels", record_id=channel_id, before=None, after=after
        )
    body: JsonDict = {
        "ok": True,
        "channel": store.channel_view(channel_id) if _bool(params, "return_im", False) else {"id": channel_id},
    }
    if not created:
        body.update({"no_op": True, "already_open": True})
    return body


# -- chat ------------------------------------------------------------------------------


@_api("chat.postMessage", write=True)
def _chat_post_message(store: SlackStore, params: JsonDict, path: str) -> JsonDict:
    reference = _string(params, "channel")
    if reference is None or not reference.strip():
        return _invalid_arguments("channel")
    reference = reference.strip()
    channel_id = store.resolve_channel(reference, allow_names=True)
    if channel_id is None and reference in store.users and reference != BOT_USER_ID:
        channel_id, created = store.open_im(reference, source_method="chat.postMessage")
        if created:
            after = store.channel_view(channel_id, admin=True)
            store.record_mutation(
                method="POST", path=path, collection="channels", record_id=channel_id, before=None, after=after
            )
    if channel_id is None:
        return _error("channel_not_found")
    if store.channels[channel_id].get("is_archived"):
        return _error("is_archived")
    if not store.is_member(channel_id, BOT_USER_ID):
        return _error("not_in_channel")
    text = _string(params, "text")
    blocks = _json_list(params, "blocks")
    attachments = _json_list(params, "attachments")
    if not text and blocks is None and attachments is None:
        return _error("no_text")
    thread_ts = _string(params, "thread_ts")
    if thread_ts is not None and store.find_message(channel_id, thread_ts) is None:
        return _error("thread_not_found")
    message = store.post_message(
        channel_id,
        text or "",
        user=BOT_USER_ID,
        thread_ts=thread_ts,
        blocks=blocks,
        attachments=attachments,
        username=_string(params, "username"),
        icon_emoji=_string(params, "icon_emoji"),
        icon_url=_string(params, "icon_url"),
    )
    store.record_mutation(
        method="POST", path=path, collection="messages", record_id=str(message["ts"]), before=None, after=message
    )
    return {"ok": True, "channel": channel_id, "ts": message["ts"], "message": message}


def _own_message(store: SlackStore, params: JsonDict, *, denial: str) -> tuple[str, JsonDict] | JsonDict:
    channel_id = _channel_or_error(store, params)
    if isinstance(channel_id, dict):
        return channel_id
    ts = _string(params, "ts")
    if not ts:
        return _invalid_arguments("ts")
    message = store.find_message(channel_id, ts)
    if message is None:
        return _error("message_not_found")
    if message.get("user") != BOT_USER_ID:
        return _error(denial)
    return channel_id, message


@_api("chat.update", write=True)
def _chat_update(store: SlackStore, params: JsonDict, path: str) -> JsonDict:
    located = _own_message(store, params, denial="cant_update_message")
    if isinstance(located, dict):
        return located
    channel_id, message = located
    text = _string(params, "text")
    blocks = _json_list(params, "blocks")
    if not text and blocks is None:
        return _error("no_text")
    before = _clone(message)
    updated = store.update_message(channel_id, str(message["ts"]), text=text, blocks=blocks)
    store.record_mutation(
        method="POST", path=path, collection="messages", record_id=str(message["ts"]), before=before, after=updated
    )
    return {"ok": True, "channel": channel_id, "ts": updated["ts"], "text": updated["text"], "message": updated}


@_api("chat.delete", write=True)
def _chat_delete(store: SlackStore, params: JsonDict, path: str) -> JsonDict:
    located = _own_message(store, params, denial="cant_delete_message")
    if isinstance(located, dict):
        return located
    channel_id, message = located
    ts = str(message["ts"])
    removed = store.delete_message(channel_id, ts)
    store.record_mutation(method="POST", path=path, collection="messages", record_id=ts, before=removed, after=None)
    return {"ok": True, "channel": channel_id, "ts": ts}


@_api("chat.getPermalink")
def _chat_get_permalink(store: SlackStore, params: JsonDict, path: str) -> JsonDict:
    channel_id = _channel_or_error(store, params)
    if isinstance(channel_id, dict):
        return channel_id
    ts = _string(params, "message_ts")
    if not ts:
        return _invalid_arguments("message_ts")
    if store.find_message(channel_id, ts) is None:
        return _error("message_not_found")
    return {"ok": True, "channel": channel_id, "permalink": _permalink(channel_id, ts)}


# -- reactions / pins ------------------------------------------------------------------


def _item_or_error(store: SlackStore, params: JsonDict) -> tuple[str, str] | JsonDict:
    channel_id = _channel_or_error(store, params)
    if isinstance(channel_id, dict):
        return channel_id
    ts = _string(params, "timestamp")
    if not ts:
        return _invalid_arguments("timestamp")
    if store.find_message(channel_id, ts) is None:
        return _error("message_not_found")
    return channel_id, ts


@_api("reactions.add", write=True)
def _reactions_add(store: SlackStore, params: JsonDict, path: str) -> JsonDict:
    name = (_string(params, "name") or "").strip().strip(":")
    if not name:
        return _error("invalid_name")
    located = _item_or_error(store, params)
    if isinstance(located, dict):
        return located
    channel_id, ts = located
    before = _clone(store.find_message(channel_id, ts))
    if not store.add_reaction(channel_id, ts, name, user=BOT_USER_ID):
        return _error("already_reacted")
    after = store.find_message(channel_id, ts)
    store.record_mutation(method="POST", path=path, collection="messages", record_id=ts, before=before, after=after)
    return {"ok": True}


@_api("pins.add", write=True)
def _pins_add(store: SlackStore, params: JsonDict, path: str) -> JsonDict:
    located = _item_or_error(store, params)
    if isinstance(located, dict):
        return located
    channel_id, ts = located
    before = _clone(store.find_message(channel_id, ts))
    if not store.pin_message(channel_id, ts, user=BOT_USER_ID):
        return _error("already_pinned")
    after = store.find_message(channel_id, ts)
    store.record_mutation(method="POST", path=path, collection="messages", record_id=ts, before=before, after=after)
    return {"ok": True}


@_api("pins.list")
def _pins_list(store: SlackStore, params: JsonDict, path: str) -> JsonDict:
    channel_id = _channel_or_error(store, params)
    if isinstance(channel_id, dict):
        return channel_id
    items = [
        {
            "channel": channel_id,
            "created": message.get("pinned_info", {}).get("pinned_ts", 0),
            "created_by": message.get("pinned_info", {}).get("pinned_by", BOT_USER_ID),
            "message": message,
            "type": "message",
        }
        for message in store.pins(channel_id)
    ]
    return {"ok": True, "items": items}


# -- search ----------------------------------------------------------------------------


@_api("search.messages")
def _search_messages(store: SlackStore, params: JsonDict, path: str) -> JsonDict:
    query = _string(params, "query")
    if query is None or not query.strip():
        return _invalid_arguments("query")
    count = _int(params, "count", 20)
    page = _int(params, "page", 1)
    if count is None or count < 1 or count > 100 or page is None or page < 1:
        return _invalid_arguments("count")
    matches = store.search_messages(query.strip())
    total = len(matches)
    page_count = max(1, -(-total // count))
    start = (page - 1) * count
    selected = matches[start : start + count]
    rendered: list[JsonDict] = []
    for channel_id, message in selected:
        record = store.channels[channel_id]
        user = str(message.get("user", ""))
        rendered.append(
            {
                "blocks": message.get("blocks", []),
                "channel": {
                    "id": channel_id,
                    "is_channel": bool(record.get("is_channel", False)),
                    "is_ext_shared": False,
                    "is_group": bool(record.get("is_group", False)),
                    "is_im": bool(record.get("is_im", False)),
                    "is_mpim": False,
                    "is_org_shared": False,
                    "is_pending_ext_shared": False,
                    "is_private": bool(record.get("is_private", False)),
                    "is_shared": False,
                    "name": record.get("name", record.get("user", "")),
                    "pending_shared": [],
                },
                "iid": det_uuid(store.seed_key, "search", channel_id, str(message["ts"])),
                "no_reactions": not message.get("reactions"),
                "permalink": _permalink(channel_id, str(message["ts"])),
                "team": TEAM_ID,
                "text": message.get("text", ""),
                "ts": message["ts"],
                "type": "message",
                "user": user,
                "username": store.users.get(user, {}).get("name", user),
            }
        )
    return {
        "ok": True,
        "query": query.strip(),
        "messages": {
            "matches": rendered,
            "pagination": {
                "first": start + 1 if selected else 0,
                "last": start + len(selected),
                "page": page,
                "page_count": page_count,
                "per_page": count,
                "total_count": total,
            },
            "paging": {"count": count, "page": page, "pages": page_count, "total": total},
            "total": total,
        },
    }


# --------------------------------------------------------------------------------------
# ASGI app
# --------------------------------------------------------------------------------------


def make_data_app(store: Store) -> Starlette:
    """The data plane: every Web API method at `/api/<method>` (GET or POST, JSON or form bodies)."""
    if not isinstance(store, SlackStore):
        raise TypeError("make_data_app expects a SlackStore")
    slack = store

    async def api(request: Request) -> Response:
        method_name = str(request.path_params["method"])
        params = query_params(request)
        body, encoding = await parse_body(request)
        if encoding == "invalid":
            return json_response(_error("invalid_json"))
        params.update(body)
        method = _METHODS.get(method_name.casefold())
        if method is None:
            return json_response(_error("unknown_method", req_method=method_name))
        if method.write and request.method != "POST":
            return json_response(_error("method_not_supported"))
        return json_response(_clone(method.handler(slack, params, f"/api/{method.name}")))

    async def not_found(request: Request) -> Response:
        return json_response({"ok": False, "error": "not_found", "path": request.url.path}, status=404)

    methods = ["GET", "POST", "PUT", "PATCH", "DELETE"]
    return Starlette(
        routes=[
            Route("/api/{method}", api, methods=["GET", "POST"]),
            Route("/", not_found, methods=methods),
            Route("/{path:path}", not_found, methods=methods),
        ]
    )


def supported_methods() -> tuple[str, ...]:
    """Every Web API method this twin serves, in registration order."""
    return tuple(method.name for method in _METHODS.values())


SPEC = TwinSpec(
    provider="slack",
    role="team_chat",
    make_store=SlackStore,
    make_data_app=make_data_app,
    notes=(
        "admin state mirrors the Arga Slack twin: messages are `events[]` envelopes, channels carry message_count",
        "calibrated routes: conversations.list, conversations.history, chat.postMessage (CRM fixture)",
        "errors are HTTP 200 {ok:false,error}; unknown /api/* -> unknown_method; outside /api -> 404 JSON",
    ),
)
