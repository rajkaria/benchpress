"""HubSpot playbook (role `hubspot_crm`) — official CRM v3 object shapes.

Candidates come from `POST /crm/v3/objects/{companies,contacts}/search` with `filterGroups`
(OR-ed groups of AND-ed filters), cast wide across the entity's exact name, each significant
token, its domains and email hosts. Search indexes can lag, so an empty search falls back to a
plain `GET /crm/v3/objects/{type}?limit=100&properties=…` filtered client-side. The only write
constructed is `PATCH /crm/v3/objects/{type}/{id}` with `{"properties": {...}}`.
"""

from __future__ import annotations

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
    extract_field,
    flat_fields,
    parse_ref,
    read_json,
    text_matches_terms,
)
from benchpress.tools import ToolBus, as_mapping

OBJECTS_PATH = "/crm/v3/objects"

# Singular resource types used in refs → the plural path segment HubSpot uses.
OBJECT_PATHS: dict[str, str] = {
    "company": "companies",
    "contact": "contacts",
    "deal": "deals",
    "ticket": "tickets",
    "note": "notes",
}
_SINGULAR: dict[str, str] = {plural: singular for singular, plural in OBJECT_PATHS.items()}

COMPANY_PROPERTIES: tuple[str, ...] = ("name", "domain", "description", "lifecyclestage", "hs_lastmodifieddate")
CONTACT_PROPERTIES: tuple[str, ...] = ("email", "firstname", "lastname", "lifecyclestage", "notes", "company")
DEFAULT_PROPERTIES: tuple[str, ...] = ("name", "description", "hs_lastmodifieddate")

SEARCH_LIMIT = 100
LIST_LIMIT = 100
MAX_FILTER_GROUPS = 5  # HubSpot caps a search at five filterGroups
MAX_SEARCH_CALLS_PER_TYPE = 2
MAX_ID_LOOKUPS = 2
MAX_POLICY_ENTITIES = 3
MAX_POLICY_RECORDS = 8

Filter = dict[str, str]
FilterGroup = dict[str, list[Filter]]


def _filter(property_name: str, operator: str, value: str) -> FilterGroup:
    return {"filters": [{"propertyName": property_name, "operator": operator, "value": value}]}


def object_path(resource_type: str) -> str | None:
    """`company` / `companies` → `companies`; unknown types → `None`."""
    folded = resource_type.strip().casefold()
    if folded in OBJECT_PATHS:
        return OBJECT_PATHS[folded]
    if folded in _SINGULAR:
        return folded
    return None


def singular_type(plural: str) -> str:
    return _SINGULAR.get(plural, plural.rstrip("s"))


def properties_for(plural: str) -> tuple[str, ...]:
    if plural == "companies":
        return COMPANY_PROPERTIES
    if plural == "contacts":
        return CONTACT_PROPERTIES
    return DEFAULT_PROPERTIES


def company_filter_groups(terms: EntityTerms) -> list[FilterGroup]:
    """Exact name, then name tokens, then domain matches — most specific first."""
    groups: list[FilterGroup] = []
    for name in terms.names[:2]:
        groups.append(_filter("name", "EQ", name))
    for token in terms.tokens:
        groups.append(_filter("name", "CONTAINS_TOKEN", token))
    for domain in terms.domains[:2]:
        groups.append(_filter("domain", "EQ", domain))
        groups.append(_filter("domain", "CONTAINS_TOKEN", domain))
    return groups[: MAX_FILTER_GROUPS * MAX_SEARCH_CALLS_PER_TYPE]


def contact_filter_groups(terms: EntityTerms) -> list[FilterGroup]:
    """Exact emails, email hosts, then company / last-name / first-name tokens."""
    groups: list[FilterGroup] = []
    for email in terms.emails[:2]:
        groups.append(_filter("email", "EQ", email))
    for domain in terms.domains[:2]:
        groups.append(_filter("email", "CONTAINS_TOKEN", f"*@{domain}"))
    for token in terms.tokens[:2]:
        groups.append(_filter("company", "CONTAINS_TOKEN", token))
    for token in terms.tokens[:2]:
        groups.append(_filter("lastname", "CONTAINS_TOKEN", token))
    for token in terms.tokens[:2]:
        groups.append(_filter("firstname", "CONTAINS_TOKEN", token))
    return groups[: MAX_FILTER_GROUPS * MAX_SEARCH_CALLS_PER_TYPE]


def record_text(record: Mapping[str, Any], plural: str) -> str:
    """The identity text of a record, for client-side filtering."""
    properties = as_mapping(record.get("properties"))
    keys = ("name", "domain") if plural == "companies" else ("email", "firstname", "lastname", "company")
    return " ".join(as_str(properties.get(key)) for key in keys if as_str(properties.get(key)))


def company_candidate(record: Mapping[str, Any]) -> Candidate | None:
    properties = as_mapping(record.get("properties"))
    resource_id = as_str(record.get("id"))
    if not resource_id:
        return None
    name = as_str(properties.get("name")).strip()
    domain = as_str(properties.get("domain")).strip().casefold()
    return Candidate(
        provider="hubspot",
        resource_type="company",
        resource_id=resource_id,
        display=name or domain or f"company {resource_id}",
        name=name or None,
        domain=domain or None,
        lifecycle=as_str(properties.get("lifecyclestage")).strip() or None,
        notes=as_str(properties.get("description")).strip(),
    )


def contact_candidate(record: Mapping[str, Any]) -> Candidate | None:
    properties = as_mapping(record.get("properties"))
    resource_id = as_str(record.get("id"))
    if not resource_id:
        return None
    email = as_str(properties.get("email")).strip().casefold()
    full_name = " ".join(
        part
        for part in (as_str(properties.get("firstname")).strip(), as_str(properties.get("lastname")).strip())
        if part
    )
    company = as_str(properties.get("company")).strip()
    notes = as_str(properties.get("notes")).strip()
    return Candidate(
        provider="hubspot",
        resource_type="contact",
        resource_id=resource_id,
        display=full_name or email or f"contact {resource_id}",
        name=full_name or None,
        domain=domain_of(email) if email else None,
        email=email or None,
        lifecycle=as_str(properties.get("lifecyclestage")).strip() or None,
        notes="\n".join(part for part in (notes, f"company: {company}" if company else "") if part),
    )


class HubSpotPlaybook(BasePlaybook):
    provider: str = "hubspot"
    role: str = "hubspot_crm"
    identity_fields: tuple[str, ...] = ("name", "domain", "email")

    # -- reads -------------------------------------------------------------------------

    async def search(
        self, bus: ToolBus, plural: str, groups: Sequence[FilterGroup], properties: Sequence[str]
    ) -> list[Mapping[str, Any]] | None:
        """Run the filter groups in chunks of `MAX_FILTER_GROUPS`; `None` when every call failed."""
        if not groups:
            return None
        results: dict[str, Mapping[str, Any]] = {}
        succeeded = False
        chunks = [groups[index : index + MAX_FILTER_GROUPS] for index in range(0, len(groups), MAX_FILTER_GROUPS)]
        for chunk in chunks[:MAX_SEARCH_CALLS_PER_TYPE]:
            if not can_read(bus):
                break
            body: dict[str, object] = {
                "filterGroups": list(chunk),
                "properties": list(properties),
                "limit": SEARCH_LIMIT,
            }
            payload = await read_json(bus, self.provider, f"{OBJECTS_PATH}/{plural}/search", method="POST", body=body)
            if payload is None:
                continue
            succeeded = True
            for record in as_records(payload.get("results")):
                record_id = as_str(record.get("id"))
                if record_id:
                    results.setdefault(record_id, record)
        return list(results.values()) if succeeded else None

    async def list_objects(self, bus: ToolBus, plural: str, properties: Sequence[str]) -> list[Mapping[str, Any]]:
        payload = await read_json(
            bus,
            self.provider,
            f"{OBJECTS_PATH}/{plural}",
            query={"limit": str(LIST_LIMIT), "properties": ",".join(properties)},
        )
        return as_records(payload.get("results")) if payload is not None else []

    async def get_object(
        self, bus: ToolBus, plural: str, object_id: str, properties: Sequence[str]
    ) -> Mapping[str, Any] | None:
        return await read_json(
            bus,
            self.provider,
            f"{OBJECTS_PATH}/{plural}/{object_id}",
            query={"properties": ",".join(properties)},
        )

    async def find_candidates(self, bus: ToolBus, entity: str, hints: Sequence[str]) -> list[Candidate]:
        terms = entity_terms(entity, hints)
        if terms.empty:
            return []
        found: dict[str, Candidate] = {}

        companies = await self.search(bus, "companies", company_filter_groups(terms), COMPANY_PROPERTIES)
        if not companies:
            listed = await self.list_objects(bus, "companies", COMPANY_PROPERTIES)
            companies = [record for record in listed if text_matches_terms(record_text(record, "companies"), terms)]
        for record in companies:
            candidate = company_candidate(record)
            if candidate is not None:
                found.setdefault(candidate.ref, candidate)

        contact_groups = contact_filter_groups(terms)
        contacts = await self.search(bus, "contacts", contact_groups, CONTACT_PROPERTIES)
        if contacts is None or (not contacts and (terms.emails or terms.domains)):
            listed = await self.list_objects(bus, "contacts", CONTACT_PROPERTIES)
            contacts = [record for record in listed if text_matches_terms(record_text(record, "contacts"), terms)]
        for record in contacts:
            candidate = contact_candidate(record)
            if candidate is not None:
                found.setdefault(candidate.ref, candidate)

        for object_id in terms.ids[:MAX_ID_LOOKUPS]:
            if f"company:{object_id}" in found or f"contact:{object_id}" in found or not object_id.isdigit():
                continue
            record = await self.get_object(bus, "companies", object_id, COMPANY_PROPERTIES)
            candidate = company_candidate(record) if record is not None else None
            if candidate is not None:
                found.setdefault(candidate.ref, candidate)
        return list(found.values())

    async def read_record(self, bus: ToolBus, ref: str) -> ProviderRecord | None:
        parsed = parse_ref(ref)
        if parsed is None:
            return None
        plural = object_path(parsed[0])
        if plural is None:
            return None
        record = await self.get_object(bus, plural, parsed[1], properties_for(plural))
        if record is None:
            return None
        fields = {"id": as_str(record.get("id")) or parsed[1]}
        fields.update(flat_fields(record.get("properties")))
        return ProviderRecord(
            provider=self.provider,
            resource_type=singular_type(plural),
            resource_id=fields["id"],
            fields=fields,
            raw=dict(record),
        )

    async def read_field(self, bus: ToolBus, ref: str, field: str) -> str | None:
        parsed = parse_ref(ref)
        if parsed is None or not field.strip():
            return None
        plural = object_path(parsed[0])
        if plural is None:
            return None
        record = await self.get_object(bus, plural, parsed[1], (field.strip(),))
        return extract_field(record, f"properties.{field.strip()}") if record is not None else None

    async def record_texts(self, bus: ToolBus, entity: str, hints: Sequence[str]) -> list[PolicySource]:
        """Description / notes text of the candidate records for one entity (bounded)."""
        sources: list[PolicySource] = []
        for candidate in (await self.find_candidates(bus, entity, hints))[:MAX_POLICY_RECORDS]:
            if not candidate.notes.strip():
                continue
            plural = OBJECT_PATHS.get(candidate.resource_type, candidate.resource_type)
            sources.append(
                PolicySource(
                    provider=self.provider,
                    resource_ref=candidate.ref,
                    title=candidate.display,
                    text=candidate.notes,
                    path=f"{OBJECTS_PATH}/{plural}/{candidate.resource_id}",
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
        if parsed is None or not action_id:
            return None
        plural = object_path(parsed[0])
        if plural is None:
            return None
        properties = {name.strip(): value for name, value in fields.items() if name.strip()}
        if not properties:
            return None
        object_id = parsed[1]
        path = f"{OBJECTS_PATH}/{plural}/{object_id}"
        names = tuple(properties)
        normalized_ref = f"{singular_type(plural)}:{object_id}"
        return Action(
            id=action_id,
            kind="update",
            provider=self.provider,
            method="PATCH",
            path=path,
            body={"properties": properties},
            body_encoding="json",
            fields=names,
            satisfies=tuple(satisfies),
            target_refs=tuple(dict.fromkeys([*target_refs, normalized_ref])),
            readback=ReadBack(
                method="GET",
                path=path,
                query={"properties": ",".join(names)},
                field_path=f"properties.{names[0]}",
            ),
            rationale=rationale or f"update {', '.join(names)} on {normalized_ref}",
        )


__all__ = [
    "COMPANY_PROPERTIES",
    "CONTACT_PROPERTIES",
    "OBJECTS_PATH",
    "OBJECT_PATHS",
    "HubSpotPlaybook",
    "company_candidate",
    "company_filter_groups",
    "contact_candidate",
    "contact_filter_groups",
    "object_path",
    "record_text",
]
