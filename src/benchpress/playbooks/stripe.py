"""Stripe playbook (role `payments`) — official REST shapes, form-encoded writes.

Candidates come from `GET /v1/customers/search` (Stripe search syntax, `name~'…' OR email~'…'`)
with a client-side-filtered `GET /v1/customers?limit=100` fallback. The only write constructed
is `POST /v1/customers/{id}` with a flat form body and an `Idempotency-Key`. Charges, invoices,
subscriptions, refunds and deletes are never offered — not even as refused actions.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from benchpress.context import Action, Candidate, ReadBack, TaskFrame
from benchpress.normalize import domain_of
from benchpress.playbooks import (
    BasePlaybook,
    EntityTerms,
    PolicySource,
    ProviderRecord,
    as_records,
    as_str,
    can_read,
    entity_terms,
    flat_fields,
    parse_ref,
    read_json,
    text_matches_terms,
)
from benchpress.tools import ToolBus, as_mapping

CUSTOMERS_PATH = "/v1/customers"
SEARCH_PATH = f"{CUSTOMERS_PATH}/search"
PAGE_LIMIT = "100"
MAX_LIST_PAGES = 3
MAX_SEARCH_PAGES = 2
MAX_SEARCH_CALLS = 2
MAX_CLAUSES_PER_QUERY = 6
MAX_ID_LOOKUPS = 2
MAX_POLICY_ENTITIES = 3
MAX_POLICY_RECORDS = 8

# Customer fields whose update is a financial or billing-configuration change. A playbook offers
# contact/identity updates only; the gate would refuse these anyway, but they are never emitted.
BLOCKED_UPDATE_FIELDS: frozenset[str] = frozenset(
    {
        "balance",
        "cash_balance",
        "coupon",
        "default_source",
        "invoice_prefix",
        "invoice_settings",
        "next_invoice_sequence",
        "promotion_code",
        "source",
        "tax",
        "tax_exempt",
        "tax_id_data",
        "test_clock",
    }
)

# Where a customer's lifecycle tends to live when a team records one. Generic vocabulary.
LIFECYCLE_METADATA_KEYS: tuple[str, ...] = (
    "lifecycle",
    "lifecyclestage",
    "lifecycle_stage",
    "status",
    "stage",
    "segment",
    "account_type",
    "type",
)
LIFECYCLE_WORDS: tuple[str, ...] = (
    "prospect",
    "customer",
    "trial",
    "sandbox",
    "test",
    "archived",
    "churned",
    "former",
    "lead",
)

_CUSTOMER_ID = re.compile(r"^cus_[A-Za-z0-9]+$")
_BRACKET_KEY = re.compile(r"\[([^\]]*)\]")


def quote(value: str) -> str:
    """Quote a value for Stripe's search query language (single quotes, backslash escapes)."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def search_clauses(terms: EntityTerms) -> list[str]:
    """`name~'…'` for the entity and its tokens, `email~'…'` for hosts and addresses."""
    clauses: list[str] = []
    for name in terms.names[:1]:
        clauses.append(f"name~{quote(name)}")
    for token in terms.tokens[:3]:
        clauses.append(f"name~{quote(token)}")
    for domain in terms.domains[:2]:
        clauses.append(f"email~{quote(domain)}")
    for email in terms.emails[:2]:
        clauses.append(f"email~{quote(email)}")
    return list(dict.fromkeys(clauses))


def lifecycle_of(record: Mapping[str, Any]) -> str | None:
    metadata = as_mapping(record.get("metadata"))
    for key in LIFECYCLE_METADATA_KEYS:
        value = as_str(metadata.get(key)).strip()
        if value:
            return value
    description = as_str(record.get("description")).casefold()
    words = [word for word in LIFECYCLE_WORDS if re.search(rf"\b{word}\b", description)]
    return words[0] if len(words) == 1 else None


def notes_of(record: Mapping[str, Any]) -> str:
    parts: list[str] = []
    description = as_str(record.get("description")).strip()
    if description:
        parts.append(description)
    metadata = as_mapping(record.get("metadata"))
    if metadata:
        parts.append("; ".join(f"{key}={as_str(value)}" for key, value in metadata.items() if as_str(value) != ""))
    return "\n".join(part for part in parts if part)


def customer_text(record: Mapping[str, Any]) -> str:
    return " ".join(as_str(record.get(key)) for key in ("name", "email") if as_str(record.get(key)))


def customer_candidate(record: Mapping[str, Any]) -> Candidate | None:
    resource_id = as_str(record.get("id"))
    if not resource_id:
        return None
    name = as_str(record.get("name")).strip()
    email = as_str(record.get("email")).strip().casefold()
    return Candidate(
        provider="stripe",
        resource_type="customer",
        resource_id=resource_id,
        display=name or email or resource_id,
        name=name or None,
        domain=domain_of(email) if email else None,
        email=email or None,
        lifecycle=lifecycle_of(record),
        notes=notes_of(record),
    )


def form_key(field: str) -> str:
    """`metadata.lifecycle` → `metadata[lifecycle]` (Stripe form nesting); flat keys unchanged."""
    head, _, rest = field.strip().partition(".")
    if not rest or "[" in field:
        return field.strip()
    return head + "".join(f"[{segment}]" for segment in rest.split("."))


def field_path_for(form_field: str) -> str:
    """`metadata[lifecycle]` → `metadata.lifecycle` for the read-back."""
    head = form_field.split("[", 1)[0]
    return ".".join([head, *_BRACKET_KEY.findall(form_field)])


class StripePlaybook(BasePlaybook):
    provider: str = "stripe"
    role: str = "payments"
    identity_fields: tuple[str, ...] = ("name", "email", "id")

    # -- reads -------------------------------------------------------------------------

    async def search(self, bus: ToolBus, clauses: Sequence[str]) -> list[Mapping[str, Any]] | None:
        """Search customers; `None` when every call failed (e.g. search unsupported)."""
        if not clauses:
            return None
        results: dict[str, Mapping[str, Any]] = {}
        succeeded = False
        chunks = [
            clauses[index : index + MAX_CLAUSES_PER_QUERY] for index in range(0, len(clauses), MAX_CLAUSES_PER_QUERY)
        ]
        for chunk in chunks[:MAX_SEARCH_CALLS]:
            page = ""
            for _ in range(MAX_SEARCH_PAGES):
                if not can_read(bus):
                    break
                query: dict[str, str] = {"query": " OR ".join(chunk), "limit": PAGE_LIMIT}
                if page:
                    query["page"] = page
                payload = await read_json(bus, self.provider, SEARCH_PATH, query=query)
                if payload is None:
                    break
                succeeded = True
                for record in as_records(payload.get("data")):
                    record_id = as_str(record.get("id"))
                    if record_id:
                        results.setdefault(record_id, record)
                page = as_str(payload.get("next_page")).strip()
                if payload.get("has_more") is not True or not page:
                    break
        return list(results.values()) if succeeded else None

    async def list_customers(self, bus: ToolBus) -> list[Mapping[str, Any]]:
        customers: dict[str, Mapping[str, Any]] = {}
        starting_after = ""
        for _ in range(MAX_LIST_PAGES):
            if not can_read(bus):
                break
            query: dict[str, str] = {"limit": PAGE_LIMIT}
            if starting_after:
                query["starting_after"] = starting_after
            payload = await read_json(bus, self.provider, CUSTOMERS_PATH, query=query)
            if payload is None:
                break
            page = as_records(payload.get("data"))
            for record in page:
                record_id = as_str(record.get("id"))
                if record_id:
                    customers.setdefault(record_id, record)
            last_id = as_str(page[-1].get("id")) if page else ""
            if payload.get("has_more") is not True or not last_id:
                break
            starting_after = last_id
        return list(customers.values())

    async def get_customer(self, bus: ToolBus, customer_id: str) -> Mapping[str, Any] | None:
        if not customer_id.strip():
            return None
        return await read_json(bus, self.provider, f"{CUSTOMERS_PATH}/{customer_id.strip()}")

    async def find_candidates(self, bus: ToolBus, entity: str, hints: Sequence[str]) -> list[Candidate]:
        terms = entity_terms(entity, hints)
        if terms.empty:
            return []
        records = await self.search(bus, search_clauses(terms))
        if not records:
            records = [
                record for record in await self.list_customers(bus) if text_matches_terms(customer_text(record), terms)
            ]
        found: dict[str, Candidate] = {}
        for record in records:
            candidate = customer_candidate(record)
            if candidate is not None:
                found.setdefault(candidate.ref, candidate)
        for customer_id in terms.ids[:MAX_ID_LOOKUPS]:
            if not _CUSTOMER_ID.match(customer_id) or f"customer:{customer_id}" in found:
                continue
            record = await self.get_customer(bus, customer_id)
            candidate = customer_candidate(record) if record is not None else None
            if candidate is not None:
                found.setdefault(candidate.ref, candidate)
        return list(found.values())

    async def read_record(self, bus: ToolBus, ref: str) -> ProviderRecord | None:
        parsed = parse_ref(ref)
        if parsed is None or parsed[0] not in {"customer", "customers"}:
            return None
        record = await self.get_customer(bus, parsed[1])
        if record is None:
            return None
        fields = flat_fields(record)
        fields.setdefault("id", parsed[1])
        metadata_text = notes_of({"metadata": record.get("metadata")})
        if metadata_text:
            fields["metadata"] = metadata_text
        return ProviderRecord(
            provider=self.provider,
            resource_type="customer",
            resource_id=fields["id"],
            fields=fields,
            raw=dict(record),
        )

    async def record_texts(self, bus: ToolBus, entity: str, hints: Sequence[str]) -> list[PolicySource]:
        """Description and metadata text of the candidate customers for one entity (bounded)."""
        sources: list[PolicySource] = []
        for candidate in (await self.find_candidates(bus, entity, hints))[:MAX_POLICY_RECORDS]:
            if not candidate.notes.strip():
                continue
            sources.append(
                PolicySource(
                    provider=self.provider,
                    resource_ref=candidate.ref,
                    title=candidate.display,
                    text=candidate.notes,
                    path=f"{CUSTOMERS_PATH}/{candidate.resource_id}",
                )
            )
        return sources

    async def policy_sources(self, bus: ToolBus, frame: TaskFrame) -> list[PolicySource]:
        sources: dict[str, PolicySource] = {}
        for entity in frame.subject_entities[:MAX_POLICY_ENTITIES]:
            if not can_read(bus):
                break
            for source in await self.record_texts(bus, entity, frame.observed_identifiers):
                sources.setdefault(source.resource_ref, source)
        return list(sources.values())

    # -- writes ------------------------------------------------------------------------

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
        parsed = parse_ref(ref)
        if parsed is None or not action_id or parsed[0] not in {"customer", "customers"}:
            return None
        body: dict[str, str] = {}
        for name, value in fields.items():
            key = form_key(name)
            if not key:
                continue
            if key.split("[", 1)[0].casefold() in BLOCKED_UPDATE_FIELDS:
                return None
            body[key] = value
        if not body:
            return None
        customer_id = parsed[1]
        path = f"{CUSTOMERS_PATH}/{customer_id}"
        names = tuple(body)
        normalized_ref = f"customer:{customer_id}"
        return Action(
            id=action_id,
            kind="update",
            provider=self.provider,
            method="POST",
            path=path,
            body=body,
            body_encoding="form",
            headers={"Idempotency-Key": f"bp-{action_id}"},
            fields=names,
            satisfies=tuple(satisfies),
            target_refs=tuple(dict.fromkeys([*target_refs, normalized_ref])),
            readback=ReadBack(method="GET", path=path, field_path=field_path_for(names[0])),
            rationale=rationale or f"update {', '.join(names)} on {normalized_ref}",
        )


__all__ = [
    "BLOCKED_UPDATE_FIELDS",
    "CUSTOMERS_PATH",
    "SEARCH_PATH",
    "StripePlaybook",
    "customer_candidate",
    "field_path_for",
    "form_key",
    "lifecycle_of",
    "quote",
    "search_clauses",
]
