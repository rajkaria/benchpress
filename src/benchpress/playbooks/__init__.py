"""Provider playbooks: how to do ordinary things against each provider's real API.

INTERFACE STUB — the concrete playbooks (slack, gmail, hubspot, stripe) are being built on a
parallel branch against exactly this interface; this file is replaced by that branch's
version at merge time. Nothing here is task-specific.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from benchpress.context import Action, Candidate, Frozen, TaskFrame
from benchpress.tools import ToolBus


class PolicySource(Frozen):
    provider: str
    resource_ref: str
    title: str = ""
    text: str
    author: str = ""
    path: str = ""


class ProviderRecord(Frozen):
    provider: str
    resource_type: str
    resource_id: str
    fields: dict[str, str]
    raw: dict[str, object] = {}


class Playbook(Protocol):
    provider: str
    role: str
    identity_fields: tuple[str, ...]

    async def policy_sources(self, bus: ToolBus, frame: TaskFrame) -> list[PolicySource]: ...

    async def find_candidates(self, bus: ToolBus, entity: str, hints: Sequence[str]) -> list[Candidate]: ...

    async def read_record(self, bus: ToolBus, ref: str) -> ProviderRecord | None: ...

    async def read_field(self, bus: ToolBus, ref: str, field: str) -> str | None: ...

    def update_action(
        self,
        action_id: str,
        ref: str,
        fields: Mapping[str, str],
        satisfies: Sequence[str],
        *,
        target_refs: Sequence[str] = (),
        rationale: str = "",
    ) -> Action | None: ...

    def message_action(
        self,
        action_id: str,
        channel_id: str,
        text: str,
        satisfies: Sequence[str],
        *,
        thread_ts: str | None = None,
        target_refs: Sequence[str] = (),
    ) -> Action | None: ...

    def draft_action(
        self,
        action_id: str,
        to: str,
        subject: str,
        body: str,
        satisfies: Sequence[str],
        *,
        target_refs: Sequence[str] = (),
    ) -> Action | None: ...

    async def resolve_channel(self, bus: ToolBus, name: str) -> str | None: ...

    async def channel_history(self, bus: ToolBus, channel_id: str, limit: int = 50) -> list[dict[str, object]]: ...

    async def list_drafts(self, bus: ToolBus) -> list[dict[str, object]]: ...

    async def list_channel_messages_since(
        self, bus: ToolBus, channel_id: str, oldest_ts: str
    ) -> list[dict[str, object]]: ...


PLAYBOOKS: dict[str, Playbook] = {}


def for_provider(name_or_role: str) -> Playbook | None:
    return PLAYBOOKS.get(name_or_role)


def available_providers() -> tuple[str, ...]:
    return tuple(sorted({playbook.provider for playbook in PLAYBOOKS.values()}))


def extract_field(payload: object, field_path: str) -> str | None:
    """Dotted path with integer segments for lists: `messages.0.text`, `properties.email`."""
    node: object = payload
    for segment in field_path.split("."):
        if isinstance(node, Mapping):
            node = dict(node).get(segment)  # type: ignore[arg-type]
        elif isinstance(node, list) and segment.isdigit():
            items: list[Any] = node  # type: ignore[assignment]
            index = int(segment)
            node = items[index] if index < len(items) else None
        else:
            return None
        if node is None:
            return None
    if isinstance(node, (str, int, float, bool)):
        return str(node)
    return None
