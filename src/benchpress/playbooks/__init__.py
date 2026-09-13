"""Provider playbooks — *how* to do ordinary things against each provider's real API.

A playbook never decides *what* to do. It exposes typed reads (through `ToolBus.read`) and pure
constructors for write `Action`s, so the phases can enumerate candidates, sweep for policy text
and plan writes without knowing any provider's wire format. Everything here is generic: the
shapes are the official API shapes (which the twins implement); no task ids, seeded names or
domains appear anywhere.

Read-back placeholders. A write's `ReadBack` may reference a value that only exists once the
write has succeeded: `{created_ts}` (the `ts` a Slack `chat.postMessage` returns) and
`{created_id}` (the `id` a Gmail draft create returns). `fill_placeholders(readback, response)`
substitutes them from the write's response body.

Field paths. `ReadBack.field_path` is a dotted path with integer segments for lists
(`messages.0.text`, `properties.description`); `extract_field` resolves one against a payload.

Every read handles a failed `ToolResult` and an exhausted budget gracefully: playbooks return
`[]` / `None` and never raise. Every playbook operation is bounded by explicit caps.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast

from pydantic import Field

from benchpress.context import Action, Candidate, Frozen, ReadBack, TaskFrame
from benchpress.normalize import canonical_email, domain_of, significant_tokens
from benchpress.tools import BudgetExhausted, ToolBus, as_mapping

CREATED_TS_PLACEHOLDER = "{created_ts}"
CREATED_ID_PLACEHOLDER = "{created_id}"

# --------------------------------------------------------------------------------------
# Models crossing phase boundaries
# --------------------------------------------------------------------------------------


class PolicySource(Frozen):
    """One piece of provider text the policy sweep should scan for operating rules."""

    provider: str
    resource_ref: str
    title: str = ""
    text: str
    author: str = ""
    path: str = ""


class ProviderRecord(Frozen):
    """A flat, string-valued view of one provider record, plus the raw payload."""

    provider: str
    resource_type: str
    resource_id: str
    fields: dict[str, str]
    raw: dict[str, object] = Field(default_factory=dict[str, object])

    @property
    def ref(self) -> str:
        return f"{self.resource_type}:{self.resource_id}"


# --------------------------------------------------------------------------------------
# The playbook contract
# --------------------------------------------------------------------------------------


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


class BasePlaybook:
    """Default no-op implementations. Each provider subclasses this and overrides what it supports."""

    provider: str = ""
    role: str = ""
    identity_fields: tuple[str, ...] = ()

    async def policy_sources(self, bus: ToolBus, frame: TaskFrame) -> list[PolicySource]:
        return []

    async def find_candidates(self, bus: ToolBus, entity: str, hints: Sequence[str]) -> list[Candidate]:
        return []

    async def read_record(self, bus: ToolBus, ref: str) -> ProviderRecord | None:
        return None

    async def read_field(self, bus: ToolBus, ref: str, field: str) -> str | None:
        record = await self.read_record(bus, ref)
        if record is None:
            return None
        return record.fields.get(field)

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


# --------------------------------------------------------------------------------------
# Shared read helpers
# --------------------------------------------------------------------------------------


def can_read(bus: ToolBus) -> bool:
    """True while the current phase still has provider calls and time left."""
    return bus.budget_left() > 0 and not bus.out_of_time()


async def read_json(
    bus: ToolBus,
    provider: str,
    path: str,
    *,
    query: Mapping[str, str] | None = None,
    method: Literal["GET", "POST"] = "GET",
    body: object | None = None,
) -> Mapping[str, Any] | None:
    """One budgeted data-plane read. `None` on transport failure, non-2xx, or an exhausted budget."""
    if not can_read(bus):
        return None
    try:
        result = await bus.read(provider, path, query=query, method=method, body=body)
    except BudgetExhausted:
        return None
    if not result.ok:
        return None
    return result.json()


def parse_ref(ref: str) -> tuple[str, str] | None:
    """Split `resource_type:id` into its parts; `None` when the ref is malformed."""
    resource_type, separator, resource_id = ref.partition(":")
    if not separator or not resource_type.strip() or not resource_id.strip():
        return None
    return resource_type.strip().casefold(), resource_id.strip()


# --------------------------------------------------------------------------------------
# Payload helpers
# --------------------------------------------------------------------------------------


def as_str(value: object) -> str:
    """A scalar rendered as text; containers become compact JSON; `None` becomes ``""``."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def as_records(value: object) -> list[Mapping[str, Any]]:
    """The mapping items of a list-shaped payload; anything else is `[]`."""
    if not isinstance(value, (list, tuple)):
        return []
    records: list[Mapping[str, Any]] = []
    for item in cast(Sequence[object], value):
        mapping = as_mapping(item)
        if mapping or isinstance(item, Mapping):
            records.append(mapping)
    return records


def flat_fields(value: object, *, prefix: str = "") -> dict[str, str]:
    """Flatten a record into dotted, string-valued keys. Scalar lists are joined with ``", "``."""
    out: dict[str, str] = {}
    if not isinstance(value, Mapping):
        return out
    for raw_key, item in cast(Mapping[object, object], value).items():
        key = f"{prefix}.{raw_key}" if prefix else str(raw_key)
        if isinstance(item, Mapping):
            out.update(flat_fields(cast(object, item), prefix=key))
        elif isinstance(item, (list, tuple)):
            items = cast(Sequence[object], item)
            if all(not isinstance(entry, (Mapping, list, tuple)) for entry in items):
                out[key] = ", ".join(as_str(entry) for entry in items if entry is not None)
            else:
                out[key] = as_str(list(items))
        elif item is not None:
            text = as_str(item)
            if text != "":
                out[key] = text
    return out


def extract_field(payload: object, field_path: str) -> str | None:
    """Resolve a dotted field path (`messages.0.text`) against a decoded payload.

    Integer segments index lists (negative indexes count from the end); every other segment is
    a mapping key. A missing key, an out-of-range index or a `None` leaf yields `None`; scalar
    leaves are rendered as text and container leaves as compact JSON. An empty path returns the
    payload itself.
    """
    current: object = payload
    if field_path:
        for segment in field_path.split("."):
            if isinstance(current, Mapping):
                mapping = cast(Mapping[object, object], current)
                if segment in mapping:
                    current = mapping[segment]
                elif _is_int(segment) and int(segment) in mapping:
                    current = mapping[int(segment)]
                else:
                    return None
            elif isinstance(current, (list, tuple)):
                items = cast(Sequence[object], current)
                if not _is_int(segment):
                    return None
                index = int(segment)
                if not -len(items) <= index < len(items):
                    return None
                current = items[index]
            else:
                return None
    if current is None:
        return None
    return as_str(current)


def _is_int(segment: str) -> bool:
    return segment.lstrip("-").isdigit() and segment.lstrip("-") != ""


def has_placeholders(readback: ReadBack) -> bool:
    haystack = readback.path + " " + " ".join(readback.query.values())
    return CREATED_TS_PLACEHOLDER in haystack or CREATED_ID_PLACEHOLDER in haystack


def fill_placeholders(readback: ReadBack, response: Mapping[str, Any]) -> ReadBack:
    """Substitute `{created_ts}` / `{created_id}` from a write's response body (`ts` / `id`)."""
    replacements = {
        CREATED_TS_PLACEHOLDER: as_str(response.get("ts")),
        CREATED_ID_PLACEHOLDER: as_str(response.get("id")),
    }

    def fill(text: str) -> str:
        for placeholder, value in replacements.items():
            if value:
                text = text.replace(placeholder, value)
        return text

    return readback.model_copy(
        update={
            "path": fill(readback.path),
            "query": {key: fill(value) for key, value in readback.query.items()},
        }
    )


# --------------------------------------------------------------------------------------
# Entity terms — how "cast wide" is spelled for every record-holding provider
# --------------------------------------------------------------------------------------

_ID_LIKE = re.compile(r"^[A-Za-z]*[_-]?[A-Za-z0-9]*\d[A-Za-z0-9_-]*$")


@dataclass(frozen=True)
class EntityTerms:
    """The search terms derived from one subject entity and the hints observed about it."""

    entity: str
    names: tuple[str, ...]
    tokens: tuple[str, ...]
    domains: tuple[str, ...]
    emails: tuple[str, ...]
    ids: tuple[str, ...]

    @property
    def empty(self) -> bool:
        return not (self.names or self.tokens or self.domains or self.emails or self.ids)


def looks_like_domain(value: str) -> bool:
    candidate = value.strip().casefold()
    return bool(candidate) and " " not in candidate and "@" not in candidate and domain_of(candidate) == candidate


def looks_like_id(value: str) -> bool:
    candidate = value.strip()
    if not candidate or " " in candidate or "@" in candidate or "." in candidate:
        return False
    return candidate.isdigit() or bool(_ID_LIKE.match(candidate))


def entity_terms(entity: str, hints: Sequence[str], *, max_tokens: int = 4) -> EntityTerms:
    """Classify the entity and its hints into names, tokens, domains, emails and ids."""
    names: list[str] = []
    domains: list[str] = []
    emails: list[str] = []
    ids: list[str] = []
    token_pool: set[str] = set()

    def classify(value: str, *, is_entity: bool) -> None:
        cleaned = value.strip()
        if not cleaned:
            return
        if "@" in cleaned and domain_of(cleaned):
            emails.append(canonical_email(cleaned))
            host = domain_of(cleaned)
            if host:
                domains.append(host)
            return
        if looks_like_domain(cleaned):
            domains.append(cleaned.casefold())
            return
        if not is_entity and looks_like_id(cleaned):
            ids.append(cleaned)
            return
        names.append(cleaned)
        token_pool.update(token for token in significant_tokens(cleaned) if len(token) >= 2)

    classify(entity, is_entity=True)
    for hint in hints:
        classify(hint, is_entity=False)

    tokens = sorted(token_pool, key=lambda token: (-len(token), token))[:max_tokens]
    return EntityTerms(
        entity=entity.strip(),
        names=tuple(dict.fromkeys(names)),
        tokens=tuple(tokens),
        domains=tuple(dict.fromkeys(domains)),
        emails=tuple(dict.fromkeys(emails)),
        ids=tuple(dict.fromkeys(ids)),
    )


def text_matches_terms(text: str, terms: EntityTerms) -> bool:
    """Client-side filter used by the list fallbacks: any token, domain or email present."""
    folded = text.casefold()
    if any(email and email in folded for email in terms.emails):
        return True
    if any(domain and domain in folded for domain in terms.domains):
        return True
    return bool(terms.tokens) and bool(set(terms.tokens) & significant_tokens(text))


# --------------------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------------------


def register(playbook: Playbook, registry: dict[str, Playbook]) -> None:
    registry[playbook.provider.casefold()] = playbook
    if playbook.role:
        registry[playbook.role.casefold()] = playbook


def _build_registry() -> dict[str, Playbook]:
    # Imported here (not at module top) because each provider module imports the base types
    # defined above; this keeps the package import acyclic.
    from benchpress.playbooks.gmail import GmailPlaybook
    from benchpress.playbooks.hubspot import HubSpotPlaybook
    from benchpress.playbooks.slack import SlackPlaybook
    from benchpress.playbooks.stripe import StripePlaybook

    registry: dict[str, Playbook] = {}
    for playbook in (SlackPlaybook(), GmailPlaybook(), HubSpotPlaybook(), StripePlaybook()):
        register(playbook, registry)
    return registry


def for_provider(name_or_role: str) -> Playbook | None:
    """Look a playbook up by provider name (`slack`) or harness role (`team_chat`)."""
    return PLAYBOOKS.get(name_or_role.strip().casefold())


def available_providers() -> tuple[str, ...]:
    return tuple(sorted({playbook.provider for playbook in PLAYBOOKS.values()}))


PLAYBOOKS: dict[str, Playbook] = _build_registry()

__all__ = [
    "CREATED_ID_PLACEHOLDER",
    "CREATED_TS_PLACEHOLDER",
    "PLAYBOOKS",
    "BasePlaybook",
    "EntityTerms",
    "Playbook",
    "PolicySource",
    "ProviderRecord",
    "as_records",
    "as_str",
    "available_providers",
    "can_read",
    "entity_terms",
    "extract_field",
    "fill_placeholders",
    "flat_fields",
    "for_provider",
    "has_placeholders",
    "looks_like_domain",
    "looks_like_id",
    "parse_ref",
    "read_json",
    "register",
    "text_matches_terms",
]
