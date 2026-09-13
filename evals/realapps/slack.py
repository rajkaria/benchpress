"""Slack seed / snapshot / reset driver: a scratch workspace, or a devsim twin by base-URL swap.

Seeded messages are posted by our bot under the seeded display name (`chat:write.customize`),
which is Slack's one disclosed departure from the published seed. Reset removes every
message the bot authored in the seeded channels; channels themselves persist (Slack has no
delete for channels, only archive) exactly like Stripe products do.

These are harness operations. The agent under test never sees this module, and the agent's
gateway never deletes anything.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast
from urllib.parse import urlsplit

import httpx

from evals.realapps.base import RealAppClient, SeedManifest, SeedResult, require_scratch_ok

SLACK_BASE_URL = "https://slack.com"
SEEDED_ICON = ":bust_in_silhouette:"
HISTORY_LIMIT = 200
MAX_PAGES = 25
RESET_GRACE_SECONDS = 5.0

_CHANNEL_TYPES = "public_channel,private_channel"


class SlackApiError(RuntimeError):
    """Slack answered `ok: false` (Slack signals API errors in the body, not the status code)."""

    def __init__(self, method: str, error: str, payload: Mapping[str, Any]) -> None:
        super().__init__(f"slack {method} failed: {error}")
        self.method = method
        self.error = error
        self.payload = dict(payload)


class SlackApp:
    """`RealApp` for Slack."""

    app = "slack"

    def __init__(self, token: str, base_url: str = SLACK_BASE_URL, *, client: RealAppClient | None = None) -> None:
        if not token:
            raise ValueError("a Slack bot token (SLACK_BOT_TOKEN) is required")
        self.base_url = base_url.rstrip("/")
        self._client = client or RealAppClient(self.base_url, {"Authorization": f"Bearer {token}"})
        self._channels: dict[str, str] = {}
        self._identity: tuple[str | None, str | None] | None = None
        self._loopback = _is_loopback(self.base_url)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> SlackApp:
        token = env.get("SLACK_BOT_TOKEN")
        if not token:
            raise ValueError("SLACK_BOT_TOKEN is not set (needed to seed/reset the scratch Slack workspace)")
        return cls(token, base_url=env.get("DEVSIM_SLACK_URL") or SLACK_BASE_URL)

    @property
    def channels(self) -> dict[str, str]:
        """Seeded channel name -> id, as far as this instance knows."""
        return dict(self._channels)

    def remember(self, manifest: SeedManifest) -> None:
        """Learn the seeded channels from an earlier manifest (for verify without reset)."""
        self._channels.update(manifest.aliases.get(self.app, {}))

    # -- RealApp -------------------------------------------------------------------------

    async def seed(self, seed_config: Mapping[str, Any], manifest: SeedManifest) -> SeedResult:
        self._guard()
        config = _mapping(seed_config.get(self.app))
        display_names = {
            str(user["name"]): str(user.get("real_name") or user["name"]) for user in _mappings(config.get("users"))
        }
        existing = await self._list_channels()
        notes: list[str] = []
        ensured = 0
        posted = 0
        for channel in _mappings(config.get("channels")):
            name = str(channel["name"])
            record = existing.get(name)
            if record is None:
                created = await self._call("conversations.create", json_body={"name": name}, tolerate=("name_taken",))
                if created.get("ok") is True:
                    record = _mapping(created.get("channel"))
                    manifest.add(self.app, "channels", str(record["id"]))
                    notes.append(f"created #{name}")
                else:
                    existing = await self._list_channels()
                    record = existing.get(name)
                    if record is None:
                        raise SlackApiError("conversations.create", "name_taken", created)
            channel_id = str(record["id"])
            if record.get("is_archived") is True:
                await self._call(
                    "conversations.unarchive", json_body={"channel": channel_id}, tolerate=("not_archived",)
                )
            await self._call(
                "conversations.join",
                json_body={"channel": channel_id},
                tolerate=("already_in_channel", "method_not_supported_for_channel_type"),
            )
            self._channels[name] = channel_id
            manifest.alias(self.app, name, channel_id)
            ensured += 1
            for message in _mappings(channel.get("messages")):
                seeded_user = str(message.get("user") or "")
                body: dict[str, Any] = {
                    "channel": channel_id,
                    "text": str(message["text"]),
                    "username": display_names.get(seeded_user, seeded_user or "Seeded User"),
                    "icon_emoji": SEEDED_ICON,
                }
                if message.get("thread_ts"):
                    body["thread_ts"] = str(message["thread_ts"])
                result = await self._call("chat.postMessage", json_body=body)
                manifest.add(self.app, "messages", f"{channel_id}:{result['ts']}")
                posted += 1
        if display_names:
            notes.append(f"{len(display_names)} seeded users impersonated via chat:write.customize")
        return SeedResult(app=self.app, counts={"channels": ensured, "messages": posted}, notes=tuple(notes))

    async def snapshot(self) -> dict[str, Any]:
        channels = await self._known_channels()
        snapshot: dict[str, Any] = {}
        for name, channel_id in sorted(channels.items()):
            payload = await self._call(
                "conversations.history", params={"channel": channel_id, "limit": str(HISTORY_LIMIT)}
            )
            snapshot[name] = {
                "id": channel_id,
                "messages": [_message_view(message) for message in _mappings(payload.get("messages"))],
            }
        return {"channels": snapshot, "users": await self._users()}

    async def reset(self, manifest: SeedManifest) -> None:
        self._guard()
        self.remember(manifest)
        oldest = f"{max(0.0, manifest.seeded_at - RESET_GRACE_SECONDS):.6f}"
        deleted: set[str] = set()
        for channel_id in sorted(set(manifest.aliases.get(self.app, {}).values())):
            for message in await self._history(channel_id, oldest=oldest):
                ts = str(message["ts"])
                if _reply_count(message) > 0:
                    for reply in await self._replies(channel_id, ts):
                        reply_ts = str(reply["ts"])
                        if reply_ts != ts and await self._is_ours(reply):
                            await self._delete(channel_id, reply_ts)
                            deleted.add(f"{channel_id}:{reply_ts}")
                if await self._is_ours(message):
                    await self._delete(channel_id, ts)
                    deleted.add(f"{channel_id}:{ts}")
        for ref in manifest.ids(self.app, "messages"):
            if ref in deleted:
                continue
            channel_id, _, ts = ref.partition(":")
            await self._delete(channel_id, ts)

    async def verify_clean(self) -> list[str]:
        residue: list[str] = []
        for name, channel_id in sorted((await self._known_channels()).items()):
            for message in await self._history(channel_id):
                ts = str(message["ts"])
                if await self._is_ours(message):
                    residue.append(f"slack:#{name}:{ts} bot message still present")
                if _reply_count(message) > 0:
                    for reply in await self._replies(channel_id, ts):
                        reply_ts = str(reply["ts"])
                        if reply_ts != ts and await self._is_ours(reply):
                            residue.append(f"slack:#{name}:{reply_ts} bot thread reply still present")
        return residue

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- Slack Web API helpers -------------------------------------------------------------

    async def identity(self) -> tuple[str | None, str | None]:
        """`(user_id, bot_id)` of the token's bot, from `auth.test`."""
        if self._identity is None:
            payload = await self._call("auth.test", json_body={})
            self._identity = (_optional_str(payload.get("user_id")), _optional_str(payload.get("bot_id")))
        return self._identity

    async def _is_ours(self, message: Mapping[str, Any]) -> bool:
        user_id, bot_id = await self.identity()
        if bot_id is not None and message.get("bot_id") == bot_id:
            return True
        if user_id is not None and message.get("user") == user_id:
            return True
        if user_id is None and bot_id is None:
            return message.get("subtype") == "bot_message" or "bot_id" in message
        return False

    async def _call(
        self,
        method: str,
        *,
        params: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
        tolerate: Sequence[str] = (),
    ) -> dict[str, Any]:
        http_method = "POST" if json_body is not None else "GET"
        response = await self._client.request(http_method, f"/api/{method}", params=params, json_body=json_body)
        payload = _json_object(response)
        if payload.get("ok") is True:
            return payload
        error = str(payload.get("error") or f"http_{response.status_code}")
        if error in tolerate:
            return payload
        raise SlackApiError(method, error, payload)

    async def _paginate(self, method: str, params: Mapping[str, str], key: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor = ""
        for _ in range(MAX_PAGES):
            page_params = dict(params)
            if cursor:
                page_params["cursor"] = cursor
            payload = await self._call(method, params=page_params)
            items.extend(_mappings(payload.get(key)))
            cursor = str(_mapping(payload.get("response_metadata")).get("next_cursor") or "")
            if not cursor:
                break
        return items

    async def _list_channels(self) -> dict[str, dict[str, Any]]:
        channels = await self._paginate(
            "conversations.list",
            {"types": _CHANNEL_TYPES, "limit": str(HISTORY_LIMIT), "exclude_archived": "false"},
            "channels",
        )
        return {str(channel["name"]): channel for channel in channels if "name" in channel}

    async def _known_channels(self) -> dict[str, str]:
        if self._channels:
            return dict(self._channels)
        listed = await self._list_channels()
        return {name: str(record["id"]) for name, record in listed.items() if record.get("is_member") is True}

    async def _history(self, channel_id: str, *, oldest: str | None = None) -> list[dict[str, Any]]:
        params = {"channel": channel_id, "limit": str(HISTORY_LIMIT)}
        if oldest is not None:
            params["oldest"] = oldest
        return await self._paginate("conversations.history", params, "messages")

    async def _replies(self, channel_id: str, ts: str) -> list[dict[str, Any]]:
        return await self._paginate(
            "conversations.replies", {"channel": channel_id, "ts": ts, "limit": str(HISTORY_LIMIT)}, "messages"
        )

    async def _users(self) -> list[dict[str, Any]]:
        members = await self._paginate("users.list", {"limit": str(HISTORY_LIMIT)}, "members")
        return [
            {
                "id": member.get("id"),
                "name": member.get("name"),
                "real_name": member.get("real_name"),
                "is_bot": member.get("is_bot"),
                "deleted": member.get("deleted"),
            }
            for member in members
        ]

    async def _delete(self, channel_id: str, ts: str) -> None:
        await self._call("chat.delete", json_body={"channel": channel_id, "ts": ts}, tolerate=("message_not_found",))

    def _guard(self) -> None:
        if not self._loopback:
            require_scratch_ok()


def from_env(env: Mapping[str, str]) -> SlackApp:
    return SlackApp.from_env(env)


# --------------------------------------------------------------------------------------


def _message_view(message: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "ts": message.get("ts"),
        "user": message.get("user"),
        "username": message.get("username"),
        "text": message.get("text"),
        "thread_ts": message.get("thread_ts"),
        "subtype": message.get("subtype"),
        "bot_id": message.get("bot_id"),
    }


def _reply_count(message: Mapping[str, Any]) -> int:
    value = message.get("reply_count")
    return value if isinstance(value, int) else 0


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = cast(object, response.json())
    except ValueError:
        return {}
    return dict(cast(Mapping[str, Any], payload)) if isinstance(payload, Mapping) else {}


def _mapping(value: object) -> dict[str, Any]:
    return dict(cast(Mapping[str, Any], value)) if isinstance(value, Mapping) else {}


def _mappings(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    return [_mapping(cast(object, item)) for item in cast(Sequence[object], value) if isinstance(item, Mapping)]


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _is_loopback(base_url: str) -> bool:
    host = (urlsplit(base_url).hostname or "").casefold()
    return host in {"localhost", "127.0.0.1", "::1"} or host.startswith("127.") or host.endswith(".localhost")
