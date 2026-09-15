"""Slack playbook (role `team_chat`) — official Web API shapes.

Slack answers HTTP 200 with `{"ok": false, "error": "…"}` on failure, so every read here checks
the `ok` flag rather than the status code. The only write this playbook constructs is
`chat.postMessage`; its read-back is `conversations.replies` on the returned `ts`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from benchpress.context import Action, ReadBack, TaskFrame
from benchpress.playbooks import (
    CREATED_TS_PLACEHOLDER,
    BasePlaybook,
    PolicySource,
    ProviderRecord,
    as_records,
    as_str,
    can_read,
    read_json,
)
from benchpress.tools import ToolBus, as_mapping

CHANNEL_TYPES = "public_channel,private_channel"
LIST_PAGE_LIMIT = "200"
MAX_LIST_PAGES = 3
HISTORY_LIMIT = 50
SINCE_LIMIT = 100
MAX_POLICY_CHANNELS = 4
MAX_MESSAGE_CHARS = 40_000

# Channel names that typically carry operating rules. Generic vocabulary, not task facts.
POLICY_CHANNEL_NAME = re.compile(r"policy|ops|announce|company|updates|guidelines", re.IGNORECASE)

_CHANNEL_ID = re.compile(r"^[CGD][A-Z0-9]{8,}$")
_SKIPPED_SUBTYPES = frozenset({"channel_join", "channel_leave", "channel_archive", "channel_unarchive"})


def normalize_channel_name(name: str) -> str:
    return name.strip().lstrip("#").strip().casefold()


def user_directory(members: Sequence[Mapping[str, object]]) -> dict[str, str]:
    """Slack user id → best display name (`profile.display_name`, `real_name`, then `name`)."""
    directory: dict[str, str] = {}
    for member in members:
        user_id = as_str(member.get("id"))
        if not user_id:
            continue
        profile = as_mapping(member.get("profile"))
        display = (
            as_str(profile.get("display_name")).strip()
            or as_str(profile.get("real_name")).strip()
            or as_str(member.get("real_name")).strip()
            or as_str(member.get("name")).strip()
        )
        if display:
            directory[user_id] = display
    return directory


def select_policy_channels(
    channels: Sequence[Mapping[str, object]],
    originating_channel: str | None,
    *,
    cap: int = MAX_POLICY_CHANNELS,
) -> list[Mapping[str, object]]:
    """The originating channel first, then policy-named live channels by name, `cap` in total."""
    wanted = normalize_channel_name(originating_channel or "")
    selected: list[Mapping[str, object]] = []
    seen: set[str] = set()

    def take(channel: Mapping[str, object]) -> None:
        channel_id = as_str(channel.get("id"))
        if channel_id and channel_id not in seen and len(selected) < cap:
            seen.add(channel_id)
            selected.append(channel)

    if wanted:
        for channel in channels:
            if normalize_channel_name(as_str(channel.get("name"))) == wanted:
                take(channel)
                break
    policy_named = [
        channel
        for channel in channels
        if POLICY_CHANNEL_NAME.search(as_str(channel.get("name"))) and channel.get("is_archived") is not True
    ]
    for channel in sorted(policy_named, key=lambda channel: as_str(channel.get("name"))):
        take(channel)
    return selected


def parse_channel_ref(ref: str) -> tuple[str, str | None] | None:
    """`channel:<id>` → (id, None); `channel:<id>/message:<ts>` → (id, ts)."""
    match = re.match(r"^channel:([^/\s]+)(?:/message:([0-9.]+))?$", ref.strip())
    if match:
        return match.group(1), match.group(2)
    match = re.match(r"^message:([^/\s]+)/([0-9.]+)$", ref.strip())
    if match:
        return match.group(1), match.group(2)
    return None


def _messages(payload: Mapping[str, Any] | None) -> list[dict[str, object]]:
    if payload is None:
        return []
    return [dict(message) for message in as_records(payload.get("messages"))]


class SlackPlaybook(BasePlaybook):
    provider: str = "slack"
    role: str = "team_chat"
    identity_fields: tuple[str, ...] = ("name", "id")

    # -- reads -------------------------------------------------------------------------

    async def call(self, bus: ToolBus, method_name: str, query: Mapping[str, str]) -> Mapping[str, Any] | None:
        """One Slack Web API read; `None` on transport failure, non-2xx, or `ok: false`."""
        payload = await read_json(bus, self.provider, f"/api/{method_name}", query=query)
        if payload is None or payload.get("ok") is False:
            return None
        return payload

    async def list_channels(self, bus: ToolBus) -> list[dict[str, object]]:
        channels: list[dict[str, object]] = []
        cursor = ""
        for _ in range(MAX_LIST_PAGES):
            query: dict[str, str] = {"types": CHANNEL_TYPES, "limit": LIST_PAGE_LIMIT}
            if cursor:
                query["cursor"] = cursor
            payload = await self.call(bus, "conversations.list", query)
            if payload is None:
                break
            channels.extend(dict(channel) for channel in as_records(payload.get("channels")))
            cursor = as_str(as_mapping(payload.get("response_metadata")).get("next_cursor")).strip()
            if not cursor:
                break
        return channels

    async def resolve_channel(self, bus: ToolBus, name: str) -> str | None:
        wanted = normalize_channel_name(name)
        if not wanted:
            return None
        channels = await self.list_channels(bus)
        for channel in channels:
            names = {
                normalize_channel_name(as_str(channel.get("name"))),
                normalize_channel_name(as_str(channel.get("name_normalized"))),
            }
            if wanted in names:
                return as_str(channel.get("id")) or None
        raw = name.strip()
        if _CHANNEL_ID.match(raw) and any(as_str(channel.get("id")) == raw for channel in channels):
            return raw
        return None

    async def channel_history(
        self, bus: ToolBus, channel_id: str, limit: int = HISTORY_LIMIT
    ) -> list[dict[str, object]]:
        if not channel_id.strip():
            return []
        bounded = str(max(1, min(limit, 999)))
        payload = await self.call(bus, "conversations.history", {"channel": channel_id.strip(), "limit": bounded})
        return _messages(payload)

    async def list_channel_messages_since(
        self, bus: ToolBus, channel_id: str, oldest_ts: str
    ) -> list[dict[str, object]]:
        if not channel_id.strip() or not oldest_ts.strip():
            return []
        payload = await self.call(
            bus,
            "conversations.history",
            {
                "channel": channel_id.strip(),
                "oldest": oldest_ts.strip(),
                "inclusive": "false",
                "limit": str(SINCE_LIMIT),
            },
        )
        return _messages(payload)

    async def replies(self, bus: ToolBus, channel_id: str, ts: str, limit: int = 1) -> list[dict[str, object]]:
        if not channel_id.strip() or not ts.strip():
            return []
        payload = await self.call(
            bus,
            "conversations.replies",
            {"channel": channel_id.strip(), "ts": ts.strip(), "limit": str(max(1, limit))},
        )
        return _messages(payload)

    async def users(self, bus: ToolBus) -> list[dict[str, object]]:
        payload = await self.call(bus, "users.list", {"limit": LIST_PAGE_LIMIT})
        if payload is None:
            return []
        return [dict(member) for member in as_records(payload.get("members"))]

    async def policy_sources(self, bus: ToolBus, frame: TaskFrame) -> list[PolicySource]:
        channels = await self.list_channels(bus)
        if not channels:
            return []
        collected: list[tuple[Mapping[str, object], Mapping[str, object]]] = []
        for channel in select_policy_channels(channels, frame.originating_channel):
            if not can_read(bus):
                break
            for message in await self.channel_history(bus, as_str(channel.get("id")), HISTORY_LIMIT):
                collected.append((channel, message))
        directory = user_directory(await self.users(bus)) if collected and can_read(bus) else {}
        sources: list[PolicySource] = []
        for channel, message in collected:
            text = as_str(message.get("text"))
            if not text.strip() or as_str(message.get("subtype")) in _SKIPPED_SUBTYPES:
                continue
            channel_id = as_str(channel.get("id"))
            channel_name = as_str(channel.get("name"))
            ts = as_str(message.get("ts"))
            user = as_str(message.get("user")) or as_str(message.get("username")) or as_str(message.get("bot_id"))
            sources.append(
                PolicySource(
                    provider=self.provider,
                    resource_ref=f"channel:{channel_id}/message:{ts}",
                    title=f"#{channel_name}" if channel_name else f"channel:{channel_id}",
                    text=text,
                    author=directory.get(user, user),
                    path=f"/api/conversations.history?channel={channel_id}",
                )
            )
        return sources

    async def read_record(self, bus: ToolBus, ref: str) -> ProviderRecord | None:
        parsed = parse_channel_ref(ref)
        if parsed is None:
            return None
        channel_id, ts = parsed
        if ts is not None:
            for message in await self.replies(bus, channel_id, ts, limit=1):
                if as_str(message.get("ts")) == ts:
                    return ProviderRecord(
                        provider=self.provider,
                        resource_type="message",
                        resource_id=f"{channel_id}/{ts}",
                        fields={
                            "channel": channel_id,
                            "ts": ts,
                            "text": as_str(message.get("text")),
                            "user": as_str(message.get("user")),
                            "thread_ts": as_str(message.get("thread_ts")),
                        },
                        raw=dict(message),
                    )
            return None
        for channel in await self.list_channels(bus):
            if as_str(channel.get("id")) == channel_id:
                return ProviderRecord(
                    provider=self.provider,
                    resource_type="channel",
                    resource_id=channel_id,
                    fields={
                        "id": channel_id,
                        "name": as_str(channel.get("name")),
                        "topic": as_str(as_mapping(channel.get("topic")).get("value")),
                        "purpose": as_str(as_mapping(channel.get("purpose")).get("value")),
                        "is_archived": as_str(channel.get("is_archived")),
                    },
                    raw=dict(channel),
                )
        return None

    # -- writes ------------------------------------------------------------------------

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
        channel = channel_id.strip()
        if not action_id or not channel or not text.strip() or len(text) > MAX_MESSAGE_CHARS:
            return None
        body: dict[str, str] = {"channel": channel, "text": text}
        fields = ["channel", "text"]
        if thread_ts and thread_ts.strip():
            body["thread_ts"] = thread_ts.strip()
            fields.append("thread_ts")
        refs = tuple(dict.fromkeys([*target_refs, f"channel:{channel}"]))
        where = f"thread {body['thread_ts']} in channel {channel}" if "thread_ts" in body else f"channel {channel}"
        return Action(
            id=action_id,
            kind="message",
            provider=self.provider,
            method="POST",
            path="/api/chat.postMessage",
            body=body,
            body_encoding="json",
            fields=tuple(fields),
            satisfies=tuple(satisfies),
            target_refs=refs,
            readback=ReadBack(
                method="GET",
                path="/api/conversations.replies",
                query={"channel": channel, "ts": CREATED_TS_PLACEHOLDER, "limit": "1"},
                field_path="messages.0.text",
                unobserved=("channel",),
            ),
            rationale=f"post a message to {where}",
        )


__all__ = [
    "HISTORY_LIMIT",
    "MAX_POLICY_CHANNELS",
    "POLICY_CHANNEL_NAME",
    "SlackPlaybook",
    "normalize_channel_name",
    "parse_channel_ref",
    "select_policy_channels",
    "user_directory",
]
