# pyright: basic
# WIP salvaged from an interrupted agent; restore strict when finished.
"""HubSpot CRM twin — provider ``hubspot``, harness role ``hubspot_crm``.

Calibrated against the *real* Arga HubSpot twin's recorded traffic in the ArgaBench CRM fixture
(``tests/fixtures/argabench_crm_legacy/historical-fable-5-high-crm.tar.gz``): object list / get /
search / create / PATCH / merge, v4 associations, properties, owners, pipelines, the 404 / 405 / 501
error envelopes and the ``/admin/state`` shape. Everything the fixture did not exercise follows the
official HubSpot CRM v3/v4 docs and is listed as uncalibrated in
``devsim/calibration/hubspot/NOTES.md``.

Behaviours deliberately mirrored from the real twin (they matter for grader fidelity):

- ``properties=`` projection returns *requested ∩ defined* properties, ``null`` for a defined but
  unset property, and silently omits stored values that have no property definition (the seeded
  contact ``notes`` and deal ``description`` are therefore invisible to GET/list/search projections
  but appear in full-record responses to POST/PATCH/merge).
- Records store ``createdate`` / ``lastmodifieddate`` / ``hs_object_id``; the *defined*
  ``hs_lastmodifieddate`` on companies and deals projects to ``null``.
- ``associations`` inside a create body are ignored (``HubSpotStore.honor_create_associations``).
- v3 association routes answer 501 ``endpoint_not_implemented``; ``GET /crm/v3/lists`` answers 405.
- ``/admin/state`` keeps the real summary shape (``objects.<type>.{active, archived, object_type_id}``
  + ``events`` sorted by id descending) and *adds* ``objects.<type>.records`` (dict keyed by id) and a
  top-level ``associations`` list so the graders' protected-record and canonicalizer scans see records.

Reads never mutate state. Every write goes through ``Store.record_mutation`` (one journal entry and
one clock tick per written record) and appends the webhook-style ``events`` the real twin emits.
"""

from __future__ import annotations

import fnmatch
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, cast

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from devsim.twins.base import Clock, Store, TwinSpec, det_hex, det_uuid, parse_body

PORTAL_ID = 12345678
APP_ID = 1000001
HUB_DOMAIN = "hubspot-twin.local"
OWNER_ID = "41629779"
USER_ID = 9876543
OWNER_EMAIL = "admin@hubspot-twin.local"
KNOWLEDGE_BASE = "https://knowledge.hubspot.com/integrations/working-with-hubspots-api"
API_ROOT = "https://api.hubapi.com"

# Portal boot (property definitions, pipelines, owner) precedes the seed window, which precedes the
# clock epoch (= `logical_now` before the first write). All derived from the store's clock, so a
# given seed always yields byte-identical state.
_BOOT_OFFSET = timedelta(seconds=60)
_SEED_WINDOW = timedelta(seconds=30)
_SEED_STEP_MS = 137

_LIST_DEFAULT_LIMIT = 10
_LIST_MAX_LIMIT = 100
_SEARCH_DEFAULT_LIMIT = 10
_SEARCH_MAX_LIMIT = 200

_RATE_LIMIT_HEADERS: dict[str, str] = {
    "x-hubspot-ratelimit-daily": "250000",
    "x-hubspot-ratelimit-daily-remaining": "250000",
    "x-hubspot-ratelimit-interval-milliseconds": "10000",
    "x-hubspot-ratelimit-max": "100",
    "x-hubspot-ratelimit-remaining": "100",
    "x-hubspot-ratelimit-secondly": "10",
    "x-hubspot-ratelimit-secondly-remaining": "10",
}

_READ_ONLY_PROPERTIES = frozenset({"hs_object_id", "hs_lastmodifieddate", "lastmodifieddate", "hs_createdate"})
_PROPERTY_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_WILDCARD = re.compile(r"[*?]")


# --------------------------------------------------------------------------------------
# Static portal model
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ObjectType:
    name: str
    type_id: str
    singular: str
    defaults: tuple[str, ...]

    @property
    def event_prefix(self) -> str:
        """The real twin names events with a naive singular: ``companie.creation``."""
        return self.name[:-1] if self.name.endswith("s") else self.name


_OBJECT_TYPES: tuple[ObjectType, ...] = (
    ObjectType(
        "contacts",
        "0-1",
        "contact",
        ("createdate", "email", "firstname", "hs_object_id", "lastmodifieddate", "lastname"),
    ),
    ObjectType("companies", "0-2", "company", ("createdate", "domain", "hs_lastmodifieddate", "hs_object_id", "name")),
    ObjectType(
        "deals",
        "0-3",
        "deal",
        (
            "amount",
            "closedate",
            "createdate",
            "dealname",
            "dealstage",
            "hs_lastmodifieddate",
            "hs_object_id",
            "pipeline",
        ),
    ),
    ObjectType(
        "tickets",
        "0-5",
        "ticket",
        (
            "content",
            "createdate",
            "hs_lastmodifieddate",
            "hs_object_id",
            "hs_pipeline",
            "hs_pipeline_stage",
            "hs_ticket_priority",
            "subject",
        ),
    ),
    ObjectType("notes", "0-46", "note", ("hs_lastmodifieddate", "hs_object_id", "hs_timestamp")),
    ObjectType("tasks", "0-27", "task", ("hs_lastmodifieddate", "hs_object_id", "hs_timestamp")),
    ObjectType("calls", "0-48", "call", ("hs_lastmodifieddate", "hs_object_id", "hs_timestamp")),
    ObjectType("emails", "0-49", "email", ("hs_lastmodifieddate", "hs_object_id", "hs_timestamp")),
    ObjectType("meetings", "0-47", "meeting", ("hs_lastmodifieddate", "hs_object_id", "hs_timestamp")),
    ObjectType("communications", "0-18", "communication", ("hs_lastmodifieddate", "hs_object_id", "hs_timestamp")),
)
OBJECT_TYPES: dict[str, ObjectType] = {item.name: item for item in _OBJECT_TYPES}
_TYPE_ALIASES: dict[str, str] = {}
for _item in _OBJECT_TYPES:
    for _alias in (_item.name, _item.type_id, _item.singular, _item.event_prefix):
        _TYPE_ALIASES[_alias] = _item.name
_TYPE_ALIASES["company"] = "companies"

# (name, label, type, fieldType, hasUniqueValue). companies / contacts / deals are the exact sets
# the real twin serves (fixture GET /crm/v3/properties/{type}); the others are docs-based and sized
# to the fixture's `properties` counts (tickets 6, notes 5, tasks 5, calls 5, emails 5, meetings 5,
# communications 6).
_PropSpec = tuple[str, str, str, str, bool]
_PROPERTY_TABLE: dict[str, tuple[_PropSpec, ...]] = {
    "companies": (
        ("name", "Company name", "string", "text", False),
        ("domain", "Company Domain Name", "string", "text", True),
        ("industry", "Industry", "enumeration", "select", False),
        ("city", "City", "string", "text", False),
        ("state", "State/Region", "string", "text", False),
        ("country", "Country/Region", "string", "text", False),
        ("phone", "Phone Number", "string", "phonenumber", False),
        ("website", "Website URL", "string", "text", False),
        ("numberofemployees", "Number of Employees", "number", "number", False),
        ("annualrevenue", "Annual Revenue", "number", "number", False),
        ("lifecyclestage", "Lifecycle Stage", "enumeration", "select", False),
        ("description", "Description", "string", "textarea", False),
        ("createdate", "Create Date", "datetime", "date", False),
        ("hs_object_id", "Record ID", "number", "number", True),
        ("hs_lastmodifieddate", "Last Modified Date", "datetime", "date", False),
    ),
    "contacts": (
        ("email", "Email", "string", "text", True),
        ("firstname", "First Name", "string", "text", False),
        ("lastname", "Last Name", "string", "text", False),
        ("phone", "Phone Number", "phone_number", "phonenumber", False),
        ("createdate", "Create Date", "datetime", "date", False),
        ("lastmodifieddate", "Last Modified Date", "datetime", "date", False),
        ("hs_object_id", "Record ID", "number", "number", True),
        ("hubspot_owner_id", "HubSpot Owner", "string", "select", False),
        ("lifecyclestage", "Lifecycle Stage", "enumeration", "select", False),
    ),
    "deals": (
        ("dealname", "Deal Name", "string", "text", False),
        ("amount", "Amount", "number", "number", False),
        ("dealstage", "Deal Stage", "enumeration", "select", False),
        ("pipeline", "Pipeline", "enumeration", "select", False),
        ("closedate", "Close Date", "date", "date", False),
        ("createdate", "Create Date", "datetime", "date", False),
        ("hs_object_id", "Record ID", "number", "number", True),
        ("hs_lastmodifieddate", "Last Modified Date", "datetime", "date", False),
    ),
    "tickets": (
        ("subject", "Ticket name", "string", "text", False),
        ("content", "Ticket description", "string", "textarea", False),
        ("hs_pipeline", "Pipeline", "enumeration", "select", False),
        ("hs_pipeline_stage", "Ticket status", "enumeration", "select", False),
        ("hs_ticket_priority", "Priority", "enumeration", "select", False),
        ("hs_object_id", "Record ID", "number", "number", True),
    ),
    "notes": (
        ("hs_note_body", "Note body", "string", "textarea", False),
        ("hs_timestamp", "Activity date", "datetime", "date", False),
        ("hubspot_owner_id", "Activity assigned to", "enumeration", "select", False),
        ("hs_object_id", "Record ID", "number", "number", True),
        ("hs_lastmodifieddate", "Last Modified Date", "datetime", "date", False),
    ),
    "tasks": (
        ("hs_task_subject", "Task Title", "string", "text", False),
        ("hs_task_body", "Notes", "string", "textarea", False),
        ("hs_task_status", "Task Status", "enumeration", "select", False),
        ("hs_timestamp", "Due date", "datetime", "date", False),
        ("hs_object_id", "Record ID", "number", "number", True),
    ),
    "calls": (
        ("hs_call_title", "Call Title", "string", "text", False),
        ("hs_call_body", "Call notes", "string", "textarea", False),
        ("hs_call_status", "Call outcome status", "enumeration", "select", False),
        ("hs_timestamp", "Activity date", "datetime", "date", False),
        ("hs_object_id", "Record ID", "number", "number", True),
    ),
    "emails": (
        ("hs_email_subject", "Email Subject", "string", "text", False),
        ("hs_email_text", "Email Body", "string", "textarea", False),
        ("hs_email_direction", "Email Direction", "enumeration", "select", False),
        ("hs_timestamp", "Activity date", "datetime", "date", False),
        ("hs_object_id", "Record ID", "number", "number", True),
    ),
    "meetings": (
        ("hs_meeting_title", "Meeting name", "string", "text", False),
        ("hs_meeting_body", "Meeting description", "string", "textarea", False),
        ("hs_meeting_start_time", "Meeting start time", "datetime", "date", False),
        ("hs_timestamp", "Activity date", "datetime", "date", False),
        ("hs_object_id", "Record ID", "number", "number", True),
    ),
    "communications": (
        ("hs_communication_body", "Communication body", "string", "textarea", False),
        ("hs_communication_channel_type", "Channel type", "enumeration", "select", False),
        ("hs_communication_logged_from", "Logged from", "enumeration", "select", False),
        ("hs_timestamp", "Activity date", "datetime", "date", False),
        ("hubspot_owner_id", "Activity assigned to", "enumeration", "select", False),
        ("hs_object_id", "Record ID", "number", "number", True),
    ),
}
_PROPERTY_GROUPS: dict[str, str] = {
    "companies": "companyinformation",
    "contacts": "contactinformation",
    "deals": "dealinformation",
    "tickets": "ticketinformation",
}

# Legacy HubSpot association type ids (docs). The real twin answered 5 for deal→company and 3 for
# deal→contact, 1 for the seeded contact→company link and 1 for every note→X link — consistent with
# this table plus a default of 1 for pairs it does not know.
_ASSOCIATION_TYPE_IDS: dict[tuple[str, str], int] = {
    ("contacts", "companies"): 1,
    ("companies", "contacts"): 2,
    ("deals", "contacts"): 3,
    ("contacts", "deals"): 4,
    ("deals", "companies"): 5,
    ("companies", "deals"): 6,
    ("contacts", "tickets"): 15,
    ("tickets", "contacts"): 16,
    ("companies", "tickets"): 25,
    ("tickets", "companies"): 26,
    ("deals", "tickets"): 27,
    ("tickets", "deals"): 28,
}
_DEFAULT_ASSOCIATION_TYPE_ID = 1

# The real twin's Sales Pipeline has six stages (no `contractsent`), see calibration NOTES.
_DEAL_STAGES: tuple[tuple[str, str, str, str], ...] = (
    ("appointmentscheduled", "Appointment scheduled", "0.2", "false"),
    ("qualifiedtobuy", "Qualified to buy", "0.4", "false"),
    ("presentationscheduled", "Presentation scheduled", "0.6", "false"),
    ("decisionmakerboughtin", "Decision maker bought-in", "0.8", "false"),
    ("closedwon", "Closed won", "1.0", "true"),
    ("closedlost", "Closed lost", "0.0", "true"),
)
_TICKET_STAGES: tuple[tuple[str, str, str], ...] = (
    ("1", "New", "OPEN"),
    ("2", "Waiting on contact", "OPEN"),
    ("3", "Waiting on us", "OPEN"),
    ("4", "Closed", "CLOSED"),
)
_LIST_PROCESSING_TYPES = frozenset({"MANUAL", "DYNAMIC", "SNAPSHOT"})


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _iso_ms(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _epoch_ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _sorted(mapping: Mapping[str, Any]) -> dict[str, Any]:
    return {key: mapping[key] for key in sorted(mapping)}


def _copy(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def _id_sort_key(value: str) -> tuple[int, str]:
    return (int(value), value) if value.isdigit() else (1 << 62, value)


def _coerce_property_value(value: object) -> str | None:
    """HubSpot stores every property value as a string; ``null``/``""`` clear it."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True)


def _parse_iso(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _scalar(value: object) -> tuple[int, float, str] | None:
    """Comparable form ``(kind, number, text)``: numbers and datetimes (epoch ms) are kind 0, strings kind 1."""
    if value is None:
        return None
    if isinstance(value, bool):
        return (1, 0.0, "true" if value else "false")
    if isinstance(value, int | float):
        return (0, float(value), "")
    text = str(value).strip()
    if not text:
        return None
    try:
        return (0, float(text), "")
    except ValueError:
        pass
    parsed = _parse_iso(text)
    if parsed is not None:
        return (0, float(_epoch_ms(parsed)), "")
    return (1, 0.0, text.casefold())


def _tokens(value: str) -> set[str]:
    lowered = value.casefold()
    found = {piece for piece in re.split(r"[^a-z0-9]+", lowered) if piece}
    found.add(lowered)
    return found


def _token_match(value: str, pattern: str) -> bool:
    wanted = pattern.casefold()
    tokens = _tokens(value)
    if _WILDCARD.search(wanted):
        return any(fnmatch.fnmatchcase(token, wanted) for token in tokens)
    return wanted in tokens


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes"}


def _csv_params(request: Request, name: str) -> list[str] | None:
    raw = request.query_params.getlist(name)
    if not raw:
        return None
    values: list[str] = []
    for chunk in raw:
        values.extend(piece.strip() for piece in chunk.split(",") if piece.strip())
    return values


def _str_list(value: object) -> list[str]:
    if isinstance(value, str):
        return [piece.strip() for piece in value.split(",") if piece.strip()]
    if isinstance(value, list):
        return [str(item) for item in cast(list[object], value) if item is not None]
    return []


def _mapping(value: object) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _mapping_list(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [cast(dict[str, Any], item) for item in cast(list[object], value) if isinstance(item, dict)]


class ApiError(Exception):
    """A HubSpot-shaped error. The app's exception handler renders the envelope."""

    def __init__(
        self,
        status: int,
        message: str,
        category: str,
        *,
        sub_category: str | None = None,
        errors: Sequence[Mapping[str, Any]] | None = None,
        links: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        raw: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.category = category
        self.sub_category = sub_category
        self.errors = [dict(item) for item in errors or ()]
        self.links = dict(links or {})
        self.headers = dict(headers or {})
        self.raw = dict(raw) if raw is not None else None


def _not_found(what: str = "resource not found") -> ApiError:
    return ApiError(404, what, "OBJECT_NOT_FOUND")


def _validation(message: str, errors: Sequence[Mapping[str, Any]] | None = None) -> ApiError:
    return ApiError(400, message, "VALIDATION_ERROR", errors=errors)


def _unknown_type(raw: str) -> ApiError:
    return _validation(f"Unable to infer object type from: {raw}")


def _property_errors(invalid: Sequence[str], read_only: Sequence[str]) -> ApiError:
    details: list[dict[str, Any]] = []
    for name in invalid:
        message = f'Property "{name}" does not exist'
        details.append(
            {
                "isValid": False,
                "message": message,
                "error": "PROPERTY_DOESNT_EXIST",
                "name": name,
                "localizedErrorMessage": message,
            }
        )
    for name in read_only:
        message = f'Property "{name}" is read only'
        details.append(
            {
                "isValid": False,
                "message": message,
                "error": "READ_ONLY_VALUE",
                "name": name,
                "localizedErrorMessage": message,
            }
        )
    summary = json.dumps(details, ensure_ascii=False)
    return _validation(f"Property values were not valid: {summary}", errors=details)


def _not_implemented(request_id: str) -> ApiError:
    return ApiError(
        501,
        "Twin endpoint is not implemented.",
        "UNSUPPORTED_ENDPOINT",
        raw={
            "code": "endpoint_not_implemented",
            "detail": "Twin endpoint is not implemented.",
            "failure": {
                "code": "endpoint_not_implemented",
                "kind": "unsupported_endpoint",
                "phase": "fidelity",
                "public_message": "Twin endpoint is not implemented.",
                "retryable": False,
            },
            "request_id": request_id,
        },
    )


# --------------------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------------------


class HubSpotStore(Store):
    """In-memory HubSpot portal: objects, associations, lists, property definitions, events."""

    provider: ClassVar[str] = "hubspot"

    def __init__(self, seed_key: str, clock: Clock | None = None) -> None:
        super().__init__(seed_key, clock)
        self.boot: datetime = self.clock.now() - _BOOT_OFFSET
        boot_iso = _iso_ms(self.boot)
        self.objects: dict[str, dict[str, dict[str, Any]]] = {}
        self.definitions: dict[str, dict[str, dict[str, Any]]] = {
            name: {spec[0]: _definition(spec, _PROPERTY_GROUPS.get(name, "engagement"), boot_iso) for spec in specs}
            for name, specs in _PROPERTY_TABLE.items()
        }
        self.associations: dict[str, dict[str, Any]] = {}
        self._association_index: dict[frozenset[tuple[str, str]], str] = {}
        self.lists: dict[str, dict[str, Any]] = {}
        self.memberships: dict[str, dict[str, str]] = {}
        self.events: list[dict[str, Any]] = []
        self.honor_create_associations = False
        self.owner: dict[str, Any] = {
            "archived": False,
            "createdAt": boot_iso,
            "email": OWNER_EMAIL,
            "firstName": "Admin",
            "id": OWNER_ID,
            "lastName": "Twin",
            "teams": [],
            "type": "PERSON",
            "updatedAt": boot_iso,
            "userId": USER_ID,
            "userIdIncludingInactive": USER_ID,
        }
        self.user: dict[str, Any] = {
            "email": OWNER_EMAIL,
            "firstName": "Admin",
            "id": str(USER_ID),
            "lastName": "Twin",
            "primaryTeamId": None,
            "roleId": "superAdmin",
            "superAdmin": True,
        }
        self.pipelines: dict[str, list[dict[str, Any]]] = {
            "deals": [_deal_pipeline(boot_iso)],
            "tickets": [_ticket_pipeline(boot_iso)],
        }

    # -- object types & definitions -----------------------------------------------------

    @staticmethod
    def object_type(raw: str) -> ObjectType | None:
        canonical = _TYPE_ALIASES.get(raw.strip().casefold())
        return OBJECT_TYPES[canonical] if canonical else None

    def is_visible(self, otype: ObjectType, name: str) -> bool:
        return name == "hs_object_id" or name in self.definitions[otype.name]

    def validate_properties(self, otype: ObjectType, properties: Mapping[str, Any]) -> None:
        invalid = [name for name in properties if not self.is_visible(otype, name)]
        read_only = [name for name in properties if name in _READ_ONLY_PROPERTIES]
        if invalid or read_only:
            raise _property_errors(invalid, read_only)

    # -- records --------------------------------------------------------------------------

    def records(self, otype: ObjectType) -> dict[str, dict[str, Any]]:
        return self.objects.setdefault(otype.name, {})

    def get(
        self, otype: ObjectType, oid: str, *, archived: bool | None = False, id_property: str | None = None
    ) -> dict[str, Any] | None:
        record = self._lookup(otype, oid, id_property)
        if record is None:
            return None
        if archived is not None and bool(record["archived"]) != archived:
            return None
        return record

    def _lookup(self, otype: ObjectType, oid: str, id_property: str | None) -> dict[str, Any] | None:
        table = self.records(otype)
        if id_property is None or id_property == "hs_object_id":
            return table.get(oid)
        wanted = oid.casefold()
        for record in table.values():
            value = record["properties"].get(id_property)
            if isinstance(value, str) and value.casefold() == wanted:
                return record
        return None

    def find(self, otype: ObjectType, name: str, value: str) -> dict[str, Any] | None:
        """First active record whose stored property equals ``value`` (case-insensitive)."""
        wanted = value.casefold()
        for record in sorted(self.records(otype).values(), key=lambda item: _id_sort_key(str(item["id"]))):
            stored = record["properties"].get(name)
            if not record["archived"] and isinstance(stored, str) and stored.casefold() == wanted:
                return record
        return None

    def full(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """The full-record response (POST / PATCH / merge): every stored property."""
        out: dict[str, Any] = {
            "archived": record["archived"],
            "createdAt": record["createdAt"],
            "id": record["id"],
            "properties": _sorted(cast(Mapping[str, Any], record["properties"])),
            "updatedAt": record["updatedAt"],
        }
        if "archivedAt" in record:
            out["archivedAt"] = record["archivedAt"]
        return _copy(_sorted(out))

    def project(
        self,
        otype: ObjectType,
        record: Mapping[str, Any],
        requested: Sequence[str] | None,
        association_types: Sequence[ObjectType] = (),
    ) -> dict[str, Any]:
        """GET / list / search view: requested ∩ defined, null when unset, undefined omitted."""
        stored = cast(Mapping[str, Any], record["properties"])
        names = list(requested) if requested is not None else list(otype.defaults)
        properties: dict[str, Any] = {name: stored.get(name) for name in names if self.is_visible(otype, name)}
        properties["hs_object_id"] = record["id"]
        out: dict[str, Any] = {
            "archived": record["archived"],
            "createdAt": record["createdAt"],
            "id": record["id"],
            "properties": _sorted(properties),
            "updatedAt": record["updatedAt"],
        }
        if "archivedAt" in record:
            out["archivedAt"] = record["archivedAt"]
        if association_types:
            out["associations"] = {
                target.name: {
                    "paging": {},
                    "results": [
                        {"id": other, "type": f"{otype.singular}_to_{target.singular}"}
                        for other, _ in self.associated(otype, str(record["id"]), target)
                    ],
                }
                for target in association_types
            }
        return _copy(_sorted(out))

    # -- write plumbing -------------------------------------------------------------------

    def _next_stamp(self) -> datetime:
        """The instant this write lands: `record_mutation` ticks the clock by exactly one second."""
        return self.clock.now() + timedelta(seconds=1)

    def _commit(
        self, method: str, path: str, collection: str, record_id: str, before: object, after: object, stamp: datetime
    ) -> None:
        self.record_mutation(
            method=method, path=path, collection=collection, record_id=record_id, before=before, after=after
        )
        # Keep the clock and the stamped record in lock-step even if a subclass changes tick size.
        if self.clock.now() != stamp:
            self.clock._now = stamp  # pyright: ignore[reportPrivateUsage]

    def _event(
        self,
        stamp: datetime,
        event_type: str,
        object_id: str,
        *,
        change_flag: str = "NEW",
        extra: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        event_id = int(self.new_id("events", kind="digits", length=10))
        payload: dict[str, Any] = {
            "appId": APP_ID,
            "attemptNumber": 0,
            "changeFlag": change_flag,
            "changeSource": "CRM",
            "eventId": event_id,
            "objectId": int(object_id) if object_id.isdigit() else object_id,
            "occurredAt": _epoch_ms(stamp),
            "portalId": PORTAL_ID,
            "sourceId": "twin",
            "subscriptionType": event_type,
        }
        payload.update(extra or {})
        event: dict[str, Any] = {
            "created_at": _iso_ms(stamp),
            "event_type": event_type,
            "id": event_id,
            "payload": _sorted(payload),
            "pending": True,
        }
        self.events.append(event)
        return event

    def _insert(self, otype: ObjectType, properties: Mapping[str, Any], stamp: datetime) -> dict[str, Any]:
        oid = self.new_id(otype.name, kind="digits", length=10)
        iso = _iso_ms(stamp)
        stored: dict[str, Any] = {}
        for name, value in properties.items():
            coerced = _coerce_property_value(value)
            if coerced is not None:
                stored[name] = coerced
        stored["createdate"] = iso
        stored["hs_object_id"] = oid
        stored["lastmodifieddate"] = iso
        record: dict[str, Any] = {
            "archived": False,
            "createdAt": iso,
            "id": oid,
            "properties": _sorted(stored),
            "updatedAt": iso,
        }
        self.records(otype)[oid] = record
        self._event(stamp, f"{otype.event_prefix}.creation", oid)
        return record

    # -- object writes ----------------------------------------------------------------------

    def create(self, otype: ObjectType, properties: Mapping[str, Any], path: str) -> dict[str, Any]:
        self.validate_properties(otype, properties)
        stamp = self._next_stamp()
        record = self._insert(otype, properties, stamp)
        self._commit("POST", path, otype.name, str(record["id"]), None, record, stamp)
        return record

    def update(self, otype: ObjectType, record: dict[str, Any], properties: Mapping[str, Any], path: str) -> None:
        self.validate_properties(otype, properties)
        before = _copy(record)
        stamp = self._next_stamp()
        iso = _iso_ms(stamp)
        stored = cast(dict[str, Any], record["properties"])
        changed: list[tuple[str, str | None]] = []
        for name, value in properties.items():
            coerced = _coerce_property_value(value)
            if coerced is None:
                if name in stored:
                    del stored[name]
                    changed.append((name, None))
            elif stored.get(name) != coerced:
                stored[name] = coerced
                changed.append((name, coerced))
        stored["lastmodifieddate"] = iso
        record["properties"] = _sorted(stored)
        record["updatedAt"] = iso
        for name, value in changed:
            self._event(
                stamp,
                f"{otype.event_prefix}.propertyChange",
                str(record["id"]),
                change_flag="UPDATED",
                extra={"propertyName": name, "propertyValue": value},
            )
        self._commit("PATCH", path, otype.name, str(record["id"]), before, record, stamp)

    def archive(self, otype: ObjectType, record: dict[str, Any], path: str, method: str = "DELETE") -> None:
        before = _copy(record)
        stamp = self._next_stamp()
        iso = _iso_ms(stamp)
        record["archived"] = True
        record["archivedAt"] = iso
        record["updatedAt"] = iso
        self._event(stamp, f"{otype.event_prefix}.deletion", str(record["id"]), change_flag="DELETED")
        self._commit(method, path, otype.name, str(record["id"]), before, record, stamp)

    def purge(self, otype: ObjectType, record: dict[str, Any], path: str) -> None:
        """GDPR delete: the record, its associations and list memberships disappear entirely."""
        before = _copy(record)
        oid = str(record["id"])
        stamp = self._next_stamp()
        for assoc_id in [
            key for key, item in self.associations.items() if oid in (item["fromObjectId"], item["toObjectId"])
        ]:
            item = self.associations.pop(assoc_id)
            self._association_index.pop(_pair_key(item), None)
        for members in self.memberships.values():
            members.pop(oid, None)
        del self.records(otype)[oid]
        self._event(stamp, f"{otype.event_prefix}.privacyDeletion", oid, change_flag="DELETED")
        self._commit("POST", path, otype.name, oid, before, None, stamp)

    def merge(self, otype: ObjectType, primary: dict[str, Any], secondary: dict[str, Any], path: str) -> None:
        """Archive ``secondary`` into ``primary``: primary wins on conflicts, links move across."""
        before_primary = _copy(primary)
        stamp = self._next_stamp()
        iso = _iso_ms(stamp)
        stored = cast(dict[str, Any], primary["properties"])
        for name, value in cast(Mapping[str, Any], secondary["properties"]).items():
            if name in _READ_ONLY_PROPERTIES or name == "createdate":
                continue
            stored.setdefault(name, value)
        primary["properties"] = _sorted(stored)
        primary["updatedAt"] = iso  # the real twin bumps updatedAt but not lastmodifieddate on merge
        primary_id, secondary_id = str(primary["id"]), str(secondary["id"])
        for assoc in list(self.associations.values()):
            for side in ("from", "to"):
                if assoc[f"{side}ObjectType"] == otype.name and assoc[f"{side}ObjectId"] == secondary_id:
                    self._association_index.pop(_pair_key(assoc), None)
                    assoc[f"{side}ObjectId"] = primary_id
                    key = _pair_key(assoc)
                    if key in self._association_index or assoc["fromObjectId"] == assoc["toObjectId"]:
                        del self.associations[assoc["id"]]
                    else:
                        self._association_index[key] = assoc["id"]
        for members in self.memberships.values():
            if secondary_id in members:
                members.setdefault(primary_id, members.pop(secondary_id))
        self._event(stamp, f"{otype.event_prefix}.merge", primary_id, change_flag="UPDATED")
        self._commit("POST", path, otype.name, primary_id, before_primary, primary, stamp)
        self.archive(otype, secondary, path, method="POST")

    # -- associations -------------------------------------------------------------------------

    def associated(self, otype: ObjectType, oid: str, target: ObjectType) -> list[tuple[str, dict[str, Any]]]:
        found: list[tuple[str, dict[str, Any]]] = []
        for assoc in self.associations.values():
            if (
                assoc["fromObjectType"] == otype.name
                and assoc["fromObjectId"] == oid
                and assoc["toObjectType"] == target.name
            ):
                found.append((str(assoc["toObjectId"]), assoc))
            elif (
                assoc["toObjectType"] == otype.name
                and assoc["toObjectId"] == oid
                and assoc["fromObjectType"] == target.name
            ):
                found.append((str(assoc["fromObjectId"]), assoc))
        found.sort(key=lambda pair: _id_sort_key(pair[0]))
        return found

    def find_association(self, otype: ObjectType, oid: str, target: ObjectType, to_id: str) -> dict[str, Any] | None:
        key = frozenset({(otype.name, oid), (target.name, to_id)})
        assoc_id = self._association_index.get(key)
        return self.associations.get(assoc_id) if assoc_id else None

    def _link(
        self, otype: ObjectType, oid: str, target: ObjectType, to_id: str, type_id: int, category: str, stamp: datetime
    ) -> dict[str, Any]:
        existing = self.find_association(otype, oid, target, to_id)
        if existing is not None:
            return existing
        assoc: dict[str, Any] = {
            "category": category,
            "createdAt": _iso_ms(stamp),
            "fromObjectId": oid,
            "fromObjectType": otype.name,
            "id": self.new_id("associations", kind="digits", length=10),
            "label": None,
            "toObjectId": to_id,
            "toObjectType": target.name,
            "typeId": type_id,
        }
        self.associations[str(assoc["id"])] = assoc
        self._association_index[_pair_key(assoc)] = str(assoc["id"])
        self._event(
            stamp,
            f"{otype.event_prefix}.associationChange",
            oid,
            change_flag="UPDATED",
            extra={
                "associationRemoved": False,
                "associationType": f"{otype.event_prefix}_TO_{target.event_prefix}".upper(),
                "fromObjectId": int(oid),
                "isPrimaryAssociation": False,
                "toObjectId": int(to_id),
            },
        )
        return assoc

    def associate(
        self,
        otype: ObjectType,
        oid: str,
        target: ObjectType,
        to_id: str,
        path: str,
        *,
        type_id: int | None = None,
        category: str = "HUBSPOT_DEFINED",
    ) -> dict[str, Any]:
        if oid == to_id and otype is target:
            raise _validation("An object cannot be associated with itself")
        resolved = type_id if type_id is not None else default_association_type_id(otype, target)
        stamp = self._next_stamp()
        before = _copy(self.find_association(otype, oid, target, to_id))
        assoc = self._link(otype, oid, target, to_id, resolved, category, stamp)
        self._commit("PUT", path, "associations", str(assoc["id"]), before, assoc, stamp)
        return assoc

    def disassociate(self, otype: ObjectType, oid: str, target: ObjectType, to_id: str, path: str) -> bool:
        assoc = self.find_association(otype, oid, target, to_id)
        if assoc is None:
            return False
        stamp = self._next_stamp()
        before = _copy(assoc)
        del self.associations[str(assoc["id"])]
        self._association_index.pop(_pair_key(assoc), None)
        self._event(
            stamp,
            f"{otype.event_prefix}.associationChange",
            oid,
            change_flag="UPDATED",
            extra={
                "associationRemoved": True,
                "associationType": f"{otype.event_prefix}_TO_{target.event_prefix}".upper(),
                "fromObjectId": int(oid),
                "isPrimaryAssociation": False,
                "toObjectId": int(to_id),
            },
        )
        self._commit("DELETE", path, "associations", str(assoc["id"]), before, None, stamp)
        return True

    # -- property definitions -------------------------------------------------------------------

    def define_property(self, otype: ObjectType, body: Mapping[str, Any], path: str) -> dict[str, Any]:
        name = str(body.get("name", "")).strip()
        if not _PROPERTY_NAME.match(name):
            raise _validation(f"Invalid property name '{name}': use lowercase letters, numbers and underscores")
        if name in self.definitions[otype.name]:
            raise ApiError(409, f"Property with name '{name}' already exists.", "OBJECT_ALREADY_EXISTS")
        ptype = str(body.get("type", "string"))
        field_type = str(body.get("fieldType", "text"))
        stamp = self._next_stamp()
        definition = _definition(
            (name, str(body.get("label", name)), ptype, field_type, bool(body.get("hasUniqueValue", False))),
            str(body.get("groupName", _PROPERTY_GROUPS.get(otype.name, "engagement"))),
            _iso_ms(stamp),
            hubspot_defined=False,
            description=str(body.get("description", "")),
            options=_mapping_list(body.get("options")),
        )
        self.definitions[otype.name][name] = definition
        self._commit("POST", path, "properties", f"{otype.name}/{name}", None, definition, stamp)
        return definition

    def update_property(self, otype: ObjectType, name: str, body: Mapping[str, Any], path: str) -> dict[str, Any]:
        definition = self.definitions[otype.name][name]
        before = _copy(definition)
        stamp = self._next_stamp()
        for key in ("label", "description", "groupName", "fieldType", "hidden", "displayOrder", "formField"):
            if key in body:
                definition[key] = body[key]
        if "options" in body:
            definition["options"] = _mapping_list(body.get("options"))
        definition["updatedAt"] = _iso_ms(stamp)
        self._commit("PATCH", path, "properties", f"{otype.name}/{name}", before, definition, stamp)
        return definition

    def archive_property(self, otype: ObjectType, name: str, path: str) -> None:
        definition = self.definitions[otype.name][name]
        if definition.get("hubspotDefined"):
            raise _validation(f"Property '{name}' is HubSpot defined and cannot be archived")
        stamp = self._next_stamp()
        del self.definitions[otype.name][name]
        self._commit("DELETE", path, "properties", f"{otype.name}/{name}", definition, None, stamp)

    # -- lists ----------------------------------------------------------------------------------

    def list_by_name(self, object_type_id: str, name: str) -> dict[str, Any] | None:
        wanted = name.casefold()
        for item in self.lists.values():
            if (
                item["objectTypeId"] == object_type_id
                and str(item["name"]).casefold() == wanted
                and not item.get("deletedAt")
            ):
                return item
        return None

    def create_list(self, body: Mapping[str, Any], path: str) -> dict[str, Any]:
        name = str(body.get("name", "")).strip()
        if not name:
            raise _validation("List name is required")
        raw_type = str(body.get("objectTypeId", "0-1"))
        otype = self.object_type(raw_type)
        if otype is None:
            raise _unknown_type(raw_type)
        processing = str(body.get("processingType", "MANUAL")).upper()
        if processing not in _LIST_PROCESSING_TYPES:
            raise _validation(f"Invalid processingType: {processing}")
        if self.list_by_name(otype.type_id, name) is not None:
            raise ApiError(409, f"A list with the name '{name}' already exists.", "OBJECT_ALREADY_EXISTS")
        stamp = self._next_stamp()
        record = self._insert_list(name, otype, processing, stamp)
        self._commit("POST", path, "lists", str(record["listId"]), None, record, stamp)
        return record

    def _insert_list(self, name: str, otype: ObjectType, processing: str, stamp: datetime) -> dict[str, Any]:
        iso = _iso_ms(stamp)
        list_id = self.new_id("lists", kind="digits", length=9)
        record: dict[str, Any] = {
            "createdAt": iso,
            "createdById": str(USER_ID),
            "filtersUpdatedAt": iso,
            "listId": list_id,
            "listVersion": 1,
            "name": name,
            "objectTypeId": otype.type_id,
            "processingStatus": "COMPLETE",
            "processingType": processing,
            "size": 0,
            "updatedAt": iso,
            "updatedById": str(USER_ID),
        }
        self.lists[list_id] = record
        self.memberships[list_id] = {}
        return record

    def change_memberships(
        self, record: dict[str, Any], add: Sequence[str], remove: Sequence[str], path: str
    ) -> dict[str, list[str]]:
        if record["processingType"] == "DYNAMIC":
            raise _validation("Cannot manually change memberships of a DYNAMIC list")
        otype = OBJECT_TYPES[_TYPE_ALIASES[str(record["objectTypeId"])]]
        members = self.memberships.setdefault(str(record["listId"]), {})
        before = {"list": _copy(record), "memberships": _copy(members)}
        stamp = self._next_stamp()
        iso = _iso_ms(stamp)
        added: list[str] = []
        removed: list[str] = []
        missing: list[str] = []
        for rid in add:
            if self.get(otype, rid) is None:
                missing.append(rid)
            elif rid not in members:
                members[rid] = iso
                added.append(rid)
        for rid in remove:
            if rid in members:
                del members[rid]
                removed.append(rid)
            elif self.get(otype, rid) is None:
                missing.append(rid)
        record["size"] = len(members)
        record["updatedAt"] = iso
        record["listVersion"] = int(record["listVersion"]) + 1
        after = {"list": _copy(record), "memberships": _copy(members)}
        self._commit("PUT", path, "lists", str(record["listId"]), before, after, stamp)
        return {"recordsIdsAdded": added, "recordIdsRemoved": removed, "recordIdsMissing": missing}

    def rename_list(self, record: dict[str, Any], name: str, path: str) -> None:
        before = _copy(record)
        stamp = self._next_stamp()
        record["name"] = name
        record["updatedAt"] = _iso_ms(stamp)
        self._commit("PUT", path, "lists", str(record["listId"]), before, record, stamp)

    def delete_list(self, record: dict[str, Any], path: str, *, restore: bool = False) -> None:
        before = _copy(record)
        stamp = self._next_stamp()
        if restore:
            record.pop("deletedAt", None)
        else:
            record["deletedAt"] = _iso_ms(stamp)
        record["updatedAt"] = _iso_ms(stamp)
        self._commit("PUT" if restore else "DELETE", path, "lists", str(record["listId"]), before, record, stamp)

    # -- seed -----------------------------------------------------------------------------------

    def seed(self, seed_config: Mapping[str, Any]) -> None:
        config = seed_config
        nested = seed_config.get("hubspot")
        if isinstance(nested, dict) and not any(key in seed_config for key in OBJECT_TYPES):
            config = cast(Mapping[str, Any], nested)
        seeded_types = [
            name for name in ("contacts", "companies", "deals", "tickets", "notes", "tasks") if name in config
        ]
        total = sum(len(_mapping_list(config.get(name))) for name in seeded_types)
        total += len(_mapping_list(config.get("associations"))) + len(_mapping_list(config.get("lists")))
        step = timedelta(milliseconds=min(_SEED_STEP_MS, int(_SEED_WINDOW.total_seconds() * 1000) // max(total, 1)))
        base = self.clock.now() - _SEED_WINDOW
        ordinal = 0

        def stamp() -> datetime:
            nonlocal ordinal
            moment = base + step * ordinal
            ordinal += 1
            return moment

        for name in seeded_types:
            otype = OBJECT_TYPES[name]
            for item in _mapping_list(config.get(name)):
                properties = _mapping(item.get("properties")) if "properties" in item else item
                self._insert(otype, properties, stamp())
        for item in _mapping_list(config.get("associations")):
            self._seed_association(item, stamp())
        for item in _mapping_list(config.get("lists")):
            self._seed_list(item, stamp())

    def _seed_association(self, item: Mapping[str, Any], moment: datetime) -> None:
        source: tuple[ObjectType, dict[str, Any]] | None = None
        target: tuple[ObjectType, dict[str, Any]] | None = None
        if "contact_email" in item and "company_domain" in item:
            contact = self.find(OBJECT_TYPES["contacts"], "email", str(item["contact_email"]))
            company = self.find(OBJECT_TYPES["companies"], "domain", str(item["company_domain"]))
            if contact is not None and company is not None:
                source, target = (OBJECT_TYPES["contacts"], contact), (OBJECT_TYPES["companies"], company)
        else:
            source = self._seed_endpoint(item, "from")
            target = self._seed_endpoint(item, "to")
        if source is None or target is None:
            return
        (from_type, from_record), (to_type, to_record) = source, target
        self._link(
            from_type,
            str(from_record["id"]),
            to_type,
            str(to_record["id"]),
            default_association_type_id(from_type, to_type),
            "HUBSPOT_DEFINED",
            moment,
        )

    def _seed_endpoint(self, item: Mapping[str, Any], side: str) -> tuple[ObjectType, dict[str, Any]] | None:
        otype = self.object_type(str(item.get(f"{side}_type", item.get(f"{side}ObjectType", ""))))
        if otype is None:
            return None
        for key, prop in ((f"{side}_id", None), (f"{side}_email", "email"), (f"{side}_domain", "domain")):
            value = item.get(key)
            if value is None:
                continue
            record = self.get(otype, str(value)) if prop is None else self.find(otype, prop, str(value))
            if record is not None:
                return otype, record
        return None

    def _seed_list(self, item: Mapping[str, Any], moment: datetime) -> None:
        name = str(item.get("name", "")).strip()
        otype = self.object_type(str(item.get("objectTypeId", item.get("object_type", "contacts"))))
        if not name or otype is None:
            return
        record = self._insert_list(name, otype, str(item.get("processingType", "MANUAL")).upper(), moment)
        members = self.memberships[str(record["listId"])]
        for email in _str_list(item.get("contact_emails", item.get("member_emails"))):
            found = self.find(OBJECT_TYPES["contacts"], "email", email)
            if found is not None and otype.name == "contacts":
                members[str(found["id"])] = _iso_ms(moment)
        for rid in _str_list(item.get("member_ids", item.get("memberships"))):
            if self.get(otype, rid) is not None:
                members[rid] = _iso_ms(moment)
        record["size"] = len(members)

    # -- admin state ----------------------------------------------------------------------------

    def admin_state(self) -> dict[str, Any]:
        objects: dict[str, Any] = {}
        for name in sorted(self.objects):
            table = self.objects[name]
            if not table:
                continue
            archived = sum(1 for record in table.values() if record["archived"])
            objects[name] = {
                "active": len(table) - archived,
                "archived": archived,
                "object_type_id": OBJECT_TYPES[name].type_id,
                "records": {oid: _sorted(table[oid]) for oid in sorted(table, key=_id_sort_key)},
            }
        lists = [
            {**_sorted(item), "id": item["listId"], "memberships": list(self.memberships.get(str(item["listId"]), {}))}
            for item in sorted(self.lists.values(), key=lambda item: _id_sort_key(str(item["listId"])))
        ]
        state: dict[str, Any] = {
            "associations": [self.associations[key] for key in sorted(self.associations, key=_id_sort_key)],
            "deliveries": [],
            "events": sorted(self.events, key=lambda event: -int(event["id"])),
            "failure_rules": [],
            "files": [],
            "forms": [],
            "generic_hits": [],
            "generic_resources": {},
            "generic_singletons": {},
            "hub": {
                "account_type": "STANDARD",
                "app_id": APP_ID,
                "hub_domain": HUB_DOMAIN,
                "hub_id": PORTAL_ID,
                "portal_id": PORTAL_ID,
            },
            "lists": lists,
            "logical_now": self.clock.iso_ms(),
            "objects": objects,
            "owners": [self.owner],
            "pipelines": self.pipelines,
            "properties": {name: len(self.definitions[name]) for name in sorted(self.definitions)},
            "rate_limiting_enabled": False,
            "seed": 1,
            "settings": {},
            "subscriptions": [],
            "users": [self.user],
        }
        return _copy(state)


def _pair_key(assoc: Mapping[str, Any]) -> frozenset[tuple[str, str]]:
    return frozenset(
        {
            (str(assoc["fromObjectType"]), str(assoc["fromObjectId"])),
            (str(assoc["toObjectType"]), str(assoc["toObjectId"])),
        }
    )


def default_association_type_id(source: ObjectType, target: ObjectType) -> int:
    return _ASSOCIATION_TYPE_IDS.get((source.name, target.name), _DEFAULT_ASSOCIATION_TYPE_ID)


def _definition(
    spec: _PropSpec,
    group: str,
    stamp: str,
    *,
    hubspot_defined: bool = True,
    description: str = "",
    options: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    name, label, ptype, field_type, unique = spec
    return {
        "archived": False,
        "calculated": False,
        "createdAt": stamp,
        "dataSensitivity": "non_sensitive",
        "description": description,
        "displayOrder": -1,
        "externalOptions": False,
        "fieldType": field_type,
        "formField": False,
        "groupName": group,
        "hasUniqueValue": unique,
        "hidden": False,
        "hubspotDefined": hubspot_defined,
        "label": label,
        "modificationMetadata": {
            "archivable": not hubspot_defined,
            "readOnlyDefinition": hubspot_defined,
            "readOnlyValue": False,
        },
        "name": name,
        "options": [dict(option) for option in options],
        "referencedObjectType": None,
        "type": ptype,
        "updatedAt": stamp,
    }


def _stage(stage_id: str, label: str, order: int, metadata: Mapping[str, str], stamp: str) -> dict[str, Any]:
    return {
        "archived": False,
        "createdAt": stamp,
        "displayOrder": order,
        "id": stage_id,
        "label": label,
        "metadata": dict(metadata),
        "updatedAt": stamp,
        "writePermissions": "CRM_PERMISSIONS_ENFORCEMENT",
    }


def _deal_pipeline(stamp: str) -> dict[str, Any]:
    return {
        "archived": False,
        "createdAt": stamp,
        "displayOrder": 0,
        "id": "default",
        "label": "Sales Pipeline",
        "stages": [
            _stage(stage_id, label, order, {"isClosed": closed, "probability": probability}, stamp)
            for order, (stage_id, label, probability, closed) in enumerate(_DEAL_STAGES)
        ],
        "updatedAt": stamp,
    }


def _ticket_pipeline(stamp: str) -> dict[str, Any]:
    return {
        "archived": False,
        "createdAt": stamp,
        "displayOrder": 0,
        "id": "0",
        "label": "Support Pipeline",
        "stages": [
            _stage(stage_id, label, order, {"ticketState": state}, stamp)
            for order, (stage_id, label, state) in enumerate(_TICKET_STAGES)
        ],
        "updatedAt": stamp,
    }


# --------------------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------------------


def _filter_matches(stored: Mapping[str, Any], spec: Mapping[str, Any]) -> bool:
    name = str(spec.get("propertyName", ""))
    operator = str(spec.get("operator", "")).upper()
    if not name or not operator:
        raise _validation("Each filter needs a propertyName and an operator")
    value = stored.get(name)
    present = value is not None and value != ""
    if operator == "HAS_PROPERTY":
        return present
    if operator == "NOT_HAS_PROPERTY":
        return not present
    wanted = spec.get("value")
    if operator in {"IN", "NOT_IN"}:
        values = {str(item).casefold() for item in cast(list[object], spec.get("values") or [])}
        hit = present and str(value).casefold() in values
        return hit if operator == "IN" else not hit
    if operator in {"EQ", "NEQ"}:
        hit = present and str(value).casefold() == str(wanted).casefold()
        return hit if operator == "EQ" else not hit
    if operator in {"CONTAINS_TOKEN", "NOT_CONTAINS_TOKEN"}:
        hit = present and _token_match(str(value), str(wanted))
        return hit if operator == "CONTAINS_TOKEN" else not hit
    if operator in {"LT", "LTE", "GT", "GTE", "BETWEEN"}:
        left = _scalar(value)
        right = _scalar(wanted)
        if left is None or right is None or left[0] != right[0]:
            return False
        if operator == "BETWEEN":
            high = _scalar(spec.get("highValue"))
            if high is None or high[0] != left[0]:
                return False
            return right <= left <= high
        if operator == "LT":
            return left < right
        if operator == "LTE":
            return left <= right
        if operator == "GT":
            return left > right
        return left >= right
    raise _validation(f"Invalid filter operator: {operator}")


def _sort_key(value: object) -> tuple[int, int, float, str]:
    scalar = _scalar(value)
    if scalar is None:
        return (1, 0, 0.0, "")
    return (0, scalar[0], scalar[1], scalar[2])


# --------------------------------------------------------------------------------------
# Data-plane app
# --------------------------------------------------------------------------------------

_Handler = Callable[[Request], Awaitable[Response]]


class _Api:
    def __init__(self, store: HubSpotStore) -> None:
        self.store = store

    # -- plumbing -------------------------------------------------------------------------

    def _request_id(self, request: Request) -> str:
        return det_hex(
            self.store.seed_key,
            "request",
            request.method,
            request.url.path,
            request.url.query,
            len(self.store.journal),
            length=32,
        )

    def _correlation_id(self, request: Request) -> str:
        return det_uuid(
            self.store.seed_key,
            "correlation",
            request.method,
            request.url.path,
            request.url.query,
            len(self.store.journal),
        )

    def _headers(self, request: Request, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        headers = dict(_RATE_LIMIT_HEADERS)
        headers["x-request-id"] = self._request_id(request)
        headers.update(extra or {})
        return headers

    def respond(self, request: Request, payload: object, status: int = 200) -> Response:
        return JSONResponse(_copy(payload), status_code=status, headers=self._headers(request))

    def empty(self, request: Request, status: int = 204) -> Response:
        return Response(status_code=status, headers=self._headers(request))

    def error_response(self, request: Request, error: ApiError) -> Response:
        if error.raw is not None:
            return JSONResponse(error.raw, status_code=error.status, headers=self._headers(request, error.headers))
        body: dict[str, Any] = {
            "category": error.category,
            "correlationId": self._correlation_id(request),
            "message": error.message,
            "status": "error",
        }
        if error.sub_category:
            body["subCategory"] = error.sub_category
        if error.errors:
            body["errors"] = error.errors
        if error.links:
            body["links"] = dict(error.links)
        return JSONResponse(_sorted(body), status_code=error.status, headers=self._headers(request, error.headers))

    async def body(self, request: Request) -> dict[str, Any]:
        parsed, encoding = await parse_body(request)
        if encoding == "invalid":
            raise _validation("Invalid input JSON on line 1, column 1: unable to parse request body")
        return parsed

    def otype(self, request: Request, key: str = "otype") -> ObjectType:
        raw = str(request.path_params[key])
        found = self.store.object_type(raw)
        if found is None:
            raise _unknown_type(raw)
        return found

    def record(self, request: Request, otype: ObjectType, oid: str) -> dict[str, Any]:
        params = request.query_params
        archived_param = params.get("archived")
        archived: bool | None = _as_bool(archived_param) if archived_param is not None else False
        record = self.store.get(otype, oid, archived=archived, id_property=params.get("idProperty"))
        if record is None:
            raise _not_found()
        return record

    def association_targets(self, request: Request) -> list[ObjectType]:
        targets: list[ObjectType] = []
        for raw in _csv_params(request, "associations") or []:
            found = self.store.object_type(raw)
            if found is not None and found not in targets:
                targets.append(found)
        return targets

    # -- objects ------------------------------------------------------------------------------

    async def list_objects(self, request: Request) -> Response:
        otype = self.otype(request)
        params = request.query_params
        limit = _bounded_int(params.get("limit"), _LIST_DEFAULT_LIMIT, _LIST_MAX_LIMIT)
        after = _offset(params.get("after"))
        archived = _as_bool(params.get("archived", "false"))
        properties = _csv_params(request, "properties")
        targets = self.association_targets(request)
        records = [
            record
            for record in sorted(self.store.records(otype).values(), key=lambda item: _id_sort_key(str(item["id"])))
            if bool(record["archived"]) == archived
        ]
        page = records[after : after + limit]
        payload: dict[str, Any] = {
            "results": [self.store.project(otype, record, properties, targets) for record in page]
        }
        if after + limit < len(records):
            query = [f"limit={limit}", f"after={after + limit}"]
            if properties:
                query.append("properties=" + ",".join(properties))
            if targets:
                query.append("associations=" + ",".join(target.name for target in targets))
            if archived:
                query.append("archived=true")
            payload["paging"] = {
                "next": {
                    "after": str(after + limit),
                    "link": f"{API_ROOT}/crm/v3/objects/{otype.name}?" + "&".join(query),
                }
            }
        return self.respond(request, payload)

    async def create_object(self, request: Request) -> Response:
        otype = self.otype(request)
        body = await self.body(request)
        properties = _mapping(body.get("properties"))
        record = self.store.create(otype, properties, request.url.path)
        if self.store.honor_create_associations:
            for spec in _mapping_list(body.get("associations")):
                self._associate_from_spec(otype, str(record["id"]), spec, request.url.path)
        return self.respond(request, self.store.full(record), 201)

    def _associate_from_spec(self, otype: ObjectType, oid: str, spec: Mapping[str, Any], path: str) -> None:
        to_id = str(_mapping(spec.get("to")).get("id", ""))
        for type_spec in _mapping_list(spec.get("types")):
            type_id = type_spec.get("associationTypeId")
            target = _target_for_type_id(
                otype, int(type_id) if isinstance(type_id, int | str) and str(type_id).isdigit() else None
            )
            if target is not None and to_id and self.store.get(target, to_id) is not None:
                self.store.associate(otype, oid, target, to_id, path, type_id=int(str(type_id)))

    async def get_object(self, request: Request) -> Response:
        otype = self.otype(request)
        record = self.record(request, otype, str(request.path_params["oid"]))
        properties = _csv_params(request, "properties")
        return self.respond(request, self.store.project(otype, record, properties, self.association_targets(request)))

    async def patch_object(self, request: Request) -> Response:
        otype = self.otype(request)
        record = self.record(request, otype, str(request.path_params["oid"]))
        body = await self.body(request)
        properties = _mapping(body.get("properties"))
        self.store.update(otype, record, properties, request.url.path)
        return self.respond(request, self.store.full(record))

    async def delete_object(self, request: Request) -> Response:
        otype = self.otype(request)
        record = self.store.get(otype, str(request.path_params["oid"]), archived=None)
        if record is None:
            raise _not_found()
        if not record["archived"]:
            self.store.archive(otype, record, request.url.path)
        return self.empty(request)

    async def search_objects(self, request: Request) -> Response:
        otype = self.otype(request)
        body = await self.body(request)
        limit = _bounded_int(body.get("limit"), _SEARCH_DEFAULT_LIMIT, _SEARCH_MAX_LIMIT)
        after = _offset(body.get("after"))
        properties = _str_list(body.get("properties")) if body.get("properties") is not None else None
        query = str(body.get("query", "") or "").casefold()
        groups = [
            [cast(Mapping[str, Any], f) for f in cast(list[object], group.get("filters") or []) if isinstance(f, dict)]
            for group in _mapping_list(body.get("filterGroups"))
        ]
        definitions = self.store.definitions[otype.name]
        matches: list[dict[str, Any]] = []
        for record in self.store.records(otype).values():
            if record["archived"]:
                continue
            stored = cast(Mapping[str, Any], record["properties"])
            if query and not any(
                query in str(value).casefold()
                for name, value in stored.items()
                if value is not None and (name in definitions or name == "hs_object_id")
            ):
                continue
            if groups and not any(all(_filter_matches(stored, spec) for spec in group) for group in groups):
                continue
            matches.append(record)
        matches.sort(key=lambda item: _id_sort_key(str(item["id"])))
        for sort in reversed(_sorts(body.get("sorts"))):
            name, descending = sort
            matches.sort(
                key=lambda item, name=name: _sort_key(cast(Mapping[str, Any], item["properties"]).get(name)),
                reverse=descending,
            )
        page = matches[after : after + limit]
        payload: dict[str, Any] = {
            "results": [self.store.project(otype, record, properties) for record in page],
            "total": len(matches),
        }
        if after + limit < len(matches):
            payload["paging"] = {"next": {"after": str(after + limit)}}
        return self.respond(request, payload)

    async def merge_objects(self, request: Request) -> Response:
        otype = self.otype(request)
        body = await self.body(request)
        primary_id = str(body.get("primaryObjectId", "")).strip()
        secondary_id = str(body.get("objectIdToMerge", "")).strip()
        if not primary_id or not secondary_id:
            raise _validation("primaryObjectId and objectIdToMerge are required")
        if primary_id == secondary_id:
            raise _validation("primaryObjectId and objectIdToMerge must differ")
        primary = self.store.get(otype, primary_id)
        secondary = self.store.get(otype, secondary_id)
        if primary is None or secondary is None:
            raise _not_found()
        self.store.merge(otype, primary, secondary, request.url.path)
        return self.respond(request, self.store.full(primary))

    async def gdpr_delete(self, request: Request) -> Response:
        otype = self.otype(request)
        body = await self.body(request)
        object_id = str(body.get("objectId", "")).strip()
        if not object_id:
            raise _validation("objectId is required")
        id_property = body.get("idProperty")
        record = self.store.get(otype, object_id, archived=None, id_property=str(id_property) if id_property else None)
        if record is None:
            raise _not_found()
        self.store.purge(otype, record, request.url.path)
        return self.empty(request)

    async def batch_read(self, request: Request) -> Response:
        otype = self.otype(request)
        body = await self.body(request)
        properties = _str_list(body.get("properties")) if body.get("properties") is not None else None
        id_property = body.get("idProperty")
        started = self.store.clock.iso_ms()
        results: list[dict[str, Any]] = []
        missing: list[str] = []
        for item in _mapping_list(body.get("inputs")):
            oid = str(item.get("id", ""))
            record = self.store.get(otype, oid, id_property=str(id_property) if id_property else None)
            if record is None:
                missing.append(oid)
            else:
                results.append(self.store.project(otype, record, properties))
        payload: dict[str, Any] = {
            "completedAt": started,
            "results": results,
            "startedAt": started,
            "status": "COMPLETE",
        }
        status = 200
        if missing:
            status = 207
            payload["numErrors"] = 1
            payload["errors"] = [
                {
                    "category": "OBJECT_NOT_FOUND",
                    "context": {"ids": missing},
                    "message": (
                        f"Could not get some {otype.singular.upper()} objects, they may be deleted or not exist. "
                        "Check that ids are valid."
                    ),
                    "status": "error",
                }
            ]
        return self.respond(request, payload, status)

    async def batch_create(self, request: Request) -> Response:
        otype = self.otype(request)
        body = await self.body(request)
        inputs = _mapping_list(body.get("inputs"))
        for item in inputs:
            self.store.validate_properties(otype, _mapping(item.get("properties")))
        started = self.store.clock.iso_ms()
        results = [
            self.store.full(self.store.create(otype, _mapping(item.get("properties")), request.url.path))
            for item in inputs
        ]
        payload = {
            "completedAt": self.store.clock.iso_ms(),
            "results": results,
            "startedAt": started,
            "status": "COMPLETE",
        }
        return self.respond(request, payload, 201)

    async def batch_update(self, request: Request) -> Response:
        otype = self.otype(request)
        body = await self.body(request)
        inputs = _mapping_list(body.get("inputs"))
        records: list[tuple[dict[str, Any], dict[str, Any]]] = []
        missing: list[str] = []
        for item in inputs:
            oid = str(item.get("id", ""))
            record = self.store.get(otype, oid)
            if record is None:
                missing.append(oid)
                continue
            properties = _mapping(item.get("properties"))
            self.store.validate_properties(otype, properties)
            records.append((record, properties))
        started = self.store.clock.iso_ms()
        results: list[dict[str, Any]] = []
        for record, properties in records:
            self.store.update(otype, record, properties, request.url.path)
            results.append(self.store.full(record))
        payload: dict[str, Any] = {
            "completedAt": self.store.clock.iso_ms(),
            "results": results,
            "startedAt": started,
            "status": "COMPLETE",
        }
        status = 200
        if missing:
            status = 207
            payload["numErrors"] = 1
            payload["errors"] = [
                {
                    "category": "OBJECT_NOT_FOUND",
                    "context": {"ids": missing},
                    "message": (
                        f"Could not update some {otype.singular.upper()} objects, they may be deleted or not exist."
                    ),
                    "status": "error",
                }
            ]
        return self.respond(request, payload, status)

    async def batch_archive(self, request: Request) -> Response:
        otype = self.otype(request)
        body = await self.body(request)
        for item in _mapping_list(body.get("inputs")):
            record = self.store.get(otype, str(item.get("id", "")))
            if record is not None:
                self.store.archive(otype, record, request.url.path, method="POST")
        return self.empty(request)

    # -- associations (v4) ------------------------------------------------------------------------

    def _association_result(self, assoc: Mapping[str, Any], other: str) -> dict[str, Any]:
        return {
            "associationTypes": [{"category": assoc["category"], "label": assoc["label"], "typeId": assoc["typeId"]}],
            "toObjectId": int(other) if other.isdigit() else other,
        }

    async def v4_list_associations(self, request: Request) -> Response:
        otype = self.otype(request)
        target = self.otype(request, "to")
        oid = str(request.path_params["oid"])
        results = [self._association_result(assoc, other) for other, assoc in self.store.associated(otype, oid, target)]
        return self.respond(request, {"paging": {}, "results": results})

    def _put_response(self, otype: ObjectType, target: ObjectType, assoc: Mapping[str, Any]) -> dict[str, Any]:
        oid, to_id = str(assoc["fromObjectId"]), str(assoc["toObjectId"])
        if assoc["fromObjectType"] != otype.name or assoc["fromObjectId"] != oid:
            oid, to_id = to_id, oid
        return {
            "fromObjectId": int(oid),
            "fromObjectTypeId": otype.type_id,
            "labels": [{"category": assoc["category"], "label": assoc["label"], "typeId": assoc["typeId"]}],
            "toObjectId": int(to_id),
            "toObjectTypeId": target.type_id,
        }

    def _endpoints(self, request: Request) -> tuple[ObjectType, str, ObjectType, str]:
        otype = self.otype(request)
        target = self.otype(request, "to")
        oid = str(request.path_params["oid"])
        to_id = str(request.path_params["toid"])
        if self.store.get(otype, oid) is None or self.store.get(target, to_id) is None:
            raise _not_found()
        return otype, oid, target, to_id

    async def v4_put_default(self, request: Request) -> Response:
        otype, oid, target, to_id = self._endpoints(request)
        assoc = self.store.associate(otype, oid, target, to_id, request.url.path)
        return self.respond(request, self._put_response(otype, target, assoc))

    async def v4_put_labeled(self, request: Request) -> Response:
        otype, oid, target, to_id = self._endpoints(request)
        body = await self.body(request)
        specs = _mapping_list(body.get("_value")) or [body]
        type_id: int | None = None
        category = "HUBSPOT_DEFINED"
        for spec in specs:
            raw_type = spec.get("associationTypeId")
            if isinstance(raw_type, int | str) and str(raw_type).isdigit():
                type_id = int(str(raw_type))
                category = str(spec.get("associationCategory", category))
        assoc = self.store.associate(otype, oid, target, to_id, request.url.path, type_id=type_id, category=category)
        return self.respond(request, self._put_response(otype, target, assoc), 201)

    async def v4_delete(self, request: Request) -> Response:
        otype = self.otype(request)
        target = self.otype(request, "to")
        self.store.disassociate(
            otype, str(request.path_params["oid"]), target, str(request.path_params["toid"]), request.url.path
        )
        return self.empty(request)

    async def v4_batch_read(self, request: Request) -> Response:
        otype = self.otype(request)
        target = self.otype(request, "to")
        body = await self.body(request)
        started = self.store.clock.iso_ms()
        results: list[dict[str, Any]] = []
        for item in _mapping_list(body.get("inputs")):
            oid = str(item.get("id", ""))
            links = self.store.associated(otype, oid, target)
            if links:
                results.append(
                    {"from": {"id": oid}, "to": [self._association_result(assoc, other) for other, assoc in links]}
                )
        return self.respond(
            request, {"completedAt": started, "results": results, "startedAt": started, "status": "COMPLETE"}
        )

    async def v4_batch_create(self, request: Request) -> Response:
        return await self._v4_batch_associate(request, labeled=True)

    async def v4_batch_default(self, request: Request) -> Response:
        return await self._v4_batch_associate(request, labeled=False)

    async def _v4_batch_associate(self, request: Request, *, labeled: bool) -> Response:
        otype = self.otype(request)
        target = self.otype(request, "to")
        body = await self.body(request)
        started = self.store.clock.iso_ms()
        results: list[dict[str, Any]] = []
        for item in _mapping_list(body.get("inputs")):
            oid = str(_mapping(item.get("from")).get("id", ""))
            to_id = str(_mapping(item.get("to")).get("id", ""))
            if self.store.get(otype, oid) is None or self.store.get(target, to_id) is None:
                raise _not_found()
            type_id: int | None = None
            category = "HUBSPOT_DEFINED"
            if labeled:
                for spec in _mapping_list(item.get("types")):
                    raw_type = spec.get("associationTypeId")
                    if isinstance(raw_type, int | str) and str(raw_type).isdigit():
                        type_id = int(str(raw_type))
                        category = str(spec.get("associationCategory", category))
            assoc = self.store.associate(
                otype, oid, target, to_id, request.url.path, type_id=type_id, category=category
            )
            results.append(self._put_response(otype, target, assoc))
        payload = {
            "completedAt": self.store.clock.iso_ms(),
            "results": results,
            "startedAt": started,
            "status": "COMPLETE",
        }
        return self.respond(request, payload, 201)

    async def v4_batch_archive(self, request: Request) -> Response:
        otype = self.otype(request)
        target = self.otype(request, "to")
        body = await self.body(request)
        for item in _mapping_list(body.get("inputs")):
            oid = str(_mapping(item.get("from")).get("id", ""))
            for to_spec in _mapping_list(item.get("to")):
                self.store.disassociate(otype, oid, target, str(to_spec.get("id", "")), request.url.path)
        return self.empty(request)

    async def v4_labels(self, request: Request) -> Response:
        otype = self.otype(request)
        target = self.otype(request, "to")
        type_id = default_association_type_id(otype, target)
        return self.respond(request, {"results": [{"category": "HUBSPOT_DEFINED", "label": None, "typeId": type_id}]})

    async def v3_not_implemented(self, request: Request) -> Response:
        raise _not_implemented(self._request_id(request))

    # -- properties ---------------------------------------------------------------------------------

    async def list_properties(self, request: Request) -> Response:
        otype = self.otype(request)
        return self.respond(request, {"results": list(self.store.definitions[otype.name].values())})

    async def create_property(self, request: Request) -> Response:
        otype = self.otype(request)
        body = await self.body(request)
        return self.respond(request, self.store.define_property(otype, body, request.url.path), 201)

    def _definition(self, otype: ObjectType, name: str) -> dict[str, Any]:
        definition = self.store.definitions[otype.name].get(name)
        if definition is None:
            raise ApiError(
                404,
                f"Property '{name}' does not exist",
                "OBJECT_NOT_FOUND",
                sub_category="PROPERTY_DOESNT_EXIST",
                links={"knowledge-base": KNOWLEDGE_BASE},
            )
        return definition

    async def get_property(self, request: Request) -> Response:
        otype = self.otype(request)
        return self.respond(request, self._definition(otype, str(request.path_params["name"])))

    async def patch_property(self, request: Request) -> Response:
        otype = self.otype(request)
        name = str(request.path_params["name"])
        self._definition(otype, name)
        body = await self.body(request)
        return self.respond(request, self.store.update_property(otype, name, body, request.url.path))

    async def delete_property(self, request: Request) -> Response:
        otype = self.otype(request)
        name = str(request.path_params["name"])
        self._definition(otype, name)
        self.store.archive_property(otype, name, request.url.path)
        return self.empty(request)

    async def batch_read_properties(self, request: Request) -> Response:
        otype = self.otype(request)
        body = await self.body(request)
        started = self.store.clock.iso_ms()
        results = [
            self.store.definitions[otype.name][name]
            for name in (str(item.get("name", "")) for item in _mapping_list(body.get("inputs")))
            if name in self.store.definitions[otype.name]
        ]
        return self.respond(
            request, {"completedAt": started, "results": results, "startedAt": started, "status": "COMPLETE"}
        )

    # -- owners & pipelines ---------------------------------------------------------------------------

    async def list_owners(self, request: Request) -> Response:
        email = request.query_params.get("email")
        owners = [self.store.owner] if email is None or email.casefold() == OWNER_EMAIL else []
        return self.respond(request, {"results": owners})

    async def get_owner(self, request: Request) -> Response:
        if str(request.path_params["oid"]) != OWNER_ID:
            raise _not_found()
        return self.respond(request, self.store.owner)

    def _pipelines(self, request: Request) -> list[dict[str, Any]]:
        otype = self.otype(request)
        pipelines = self.store.pipelines.get(otype.name)
        if pipelines is None:
            raise _validation(f"Object type {otype.name} does not support pipelines")
        return pipelines

    def _pipeline(self, request: Request) -> dict[str, Any]:
        wanted = str(request.path_params["pid"])
        for pipeline in self._pipelines(request):
            if pipeline["id"] == wanted:
                return pipeline
        raise _not_found()

    async def list_pipelines(self, request: Request) -> Response:
        return self.respond(request, {"results": self._pipelines(request)})

    async def get_pipeline(self, request: Request) -> Response:
        return self.respond(request, self._pipeline(request))

    async def list_stages(self, request: Request) -> Response:
        return self.respond(request, {"results": self._pipeline(request)["stages"]})

    async def get_stage(self, request: Request) -> Response:
        wanted = str(request.path_params["sid"])
        for stage in cast(list[dict[str, Any]], self._pipeline(request)["stages"]):
            if stage["id"] == wanted:
                return self.respond(request, stage)
        raise _not_found()

    # -- lists ------------------------------------------------------------------------------------------

    def _list(self, request: Request) -> dict[str, Any]:
        record = self.store.lists.get(str(request.path_params["lid"]))
        if record is None or record.get("deletedAt"):
            raise ApiError(404, "List not found", "OBJECT_NOT_FOUND")
        return record

    async def lists_method_not_allowed(self, request: Request) -> Response:
        raise ApiError(
            405,
            "Method Not Allowed",
            "METHOD_NOT_ALLOWED",
            headers={"allow": "POST"},
            raw={"detail": "Method Not Allowed"},
        )

    async def create_list(self, request: Request) -> Response:
        body = await self.body(request)
        return self.respond(request, {"list": self.store.create_list(body, request.url.path)})

    async def get_list(self, request: Request) -> Response:
        return self.respond(request, {"list": self._list(request)})

    async def get_list_by_name(self, request: Request) -> Response:
        otype = self.store.object_type(str(request.path_params["tid"]))
        if otype is None:
            raise _unknown_type(str(request.path_params["tid"]))
        record = self.store.list_by_name(otype.type_id, str(request.path_params["name"]))
        if record is None:
            raise ApiError(404, "List not found", "OBJECT_NOT_FOUND")
        return self.respond(request, {"list": record})

    async def search_lists(self, request: Request) -> Response:
        body = await self.body(request)
        query = str(body.get("query", "") or "").casefold()
        types = {str(item).upper() for item in cast(list[object], body.get("processingTypes") or [])}
        wanted_ids = {str(item) for item in cast(list[object], body.get("listIds") or [])}
        count = _bounded_int(body.get("count"), 20, 500)
        offset = _offset(body.get("offset"))
        found = [
            record
            for record in sorted(self.store.lists.values(), key=lambda item: _id_sort_key(str(item["listId"])))
            if not record.get("deletedAt")
            and (not query or query in str(record["name"]).casefold())
            and (not types or record["processingType"] in types)
            and (not wanted_ids or str(record["listId"]) in wanted_ids)
        ]
        page = found[offset : offset + count]
        return self.respond(
            request,
            {"hasMore": offset + count < len(found), "lists": page, "offset": offset + len(page), "total": len(found)},
        )

    async def delete_list(self, request: Request) -> Response:
        self.store.delete_list(self._list(request), request.url.path)
        return self.empty(request)

    async def restore_list(self, request: Request) -> Response:
        record = self.store.lists.get(str(request.path_params["lid"]))
        if record is None:
            raise ApiError(404, "List not found", "OBJECT_NOT_FOUND")
        self.store.delete_list(record, request.url.path, restore=True)
        return self.empty(request)

    async def rename_list(self, request: Request) -> Response:
        record = self._list(request)
        name = request.query_params.get("listName", "").strip()
        if not name:
            raise _validation("listName is required")
        self.store.rename_list(record, name, request.url.path)
        return self.respond(request, {"list": record})

    async def list_memberships(self, request: Request) -> Response:
        record = self._list(request)
        members = self.store.memberships.get(str(record["listId"]), {})
        limit = _bounded_int(request.query_params.get("limit"), 100, 250)
        after = _offset(request.query_params.get("after"))
        ordered = list(members.items())
        page = ordered[after : after + limit]
        payload: dict[str, Any] = {
            "hasMore": after + limit < len(ordered),
            "offset": after + len(page),
            "results": [{"membershipTimestamp": stamp, "recordId": rid} for rid, stamp in page],
            "total": len(ordered),
        }
        if after + limit < len(ordered):
            payload["paging"] = {"next": {"after": str(after + limit)}}
        return self.respond(request, payload)

    async def add_memberships(self, request: Request) -> Response:
        record = self._list(request)
        body = await self.body(request)
        ids = _str_list(body.get("_value"))
        return self.respond(request, self.store.change_memberships(record, ids, (), request.url.path))

    async def remove_memberships(self, request: Request) -> Response:
        record = self._list(request)
        body = await self.body(request)
        ids = _str_list(body.get("_value"))
        return self.respond(request, self.store.change_memberships(record, (), ids, request.url.path))

    async def add_and_remove_memberships(self, request: Request) -> Response:
        record = self._list(request)
        body = await self.body(request)
        result = self.store.change_memberships(
            record, _str_list(body.get("recordIdsToAdd")), _str_list(body.get("recordIdsToRemove")), request.url.path
        )
        return self.respond(request, result)

    # -- routing ------------------------------------------------------------------------------------------

    def routes(self) -> list[Route]:
        objects = "/crm/v3/objects/{otype}"
        v4 = "/crm/v4/objects/{otype}/{oid}/associations"
        return [
            Route(objects, self.list_objects, methods=["GET"]),
            Route(objects, self.create_object, methods=["POST"]),
            Route(f"{objects}/search", self.search_objects, methods=["POST"]),
            Route(f"{objects}/merge", self.merge_objects, methods=["POST"]),
            Route(f"{objects}/gdpr-delete", self.gdpr_delete, methods=["POST"]),
            Route(f"{objects}/batch/read", self.batch_read, methods=["POST"]),
            Route(f"{objects}/batch/create", self.batch_create, methods=["POST"]),
            Route(f"{objects}/batch/update", self.batch_update, methods=["POST"]),
            Route(f"{objects}/batch/archive", self.batch_archive, methods=["POST"]),
            Route(f"{objects}/{{oid}}", self.get_object, methods=["GET"]),
            Route(f"{objects}/{{oid}}", self.patch_object, methods=["PATCH"]),
            Route(f"{objects}/{{oid}}", self.delete_object, methods=["DELETE"]),
            Route(
                f"{objects}/{{oid}}/associations/{{rest:path}}",
                self.v3_not_implemented,
                methods=["GET", "POST", "PUT", "DELETE"],
            ),
            Route(
                "/crm/v3/associations/{rest:path}", self.v3_not_implemented, methods=["GET", "POST", "PUT", "DELETE"]
            ),
            Route(f"{v4}/{{to}}", self.v4_list_associations, methods=["GET"]),
            Route(f"{v4}/default/{{to}}/{{toid}}", self.v4_put_default, methods=["PUT"]),
            Route(f"{v4}/{{to}}/{{toid}}", self.v4_put_labeled, methods=["PUT"]),
            Route(f"{v4}/{{to}}/{{toid}}", self.v4_delete, methods=["DELETE"]),
            Route("/crm/v4/associations/{otype}/{to}/batch/read", self.v4_batch_read, methods=["POST"]),
            Route("/crm/v4/associations/{otype}/{to}/batch/create", self.v4_batch_create, methods=["POST"]),
            Route("/crm/v4/associations/{otype}/{to}/batch/associate/default", self.v4_batch_default, methods=["POST"]),
            Route("/crm/v4/associations/{otype}/{to}/batch/archive", self.v4_batch_archive, methods=["POST"]),
            Route("/crm/v4/associations/{otype}/{to}/labels", self.v4_labels, methods=["GET"]),
            Route("/crm/v3/properties/{otype}", self.list_properties, methods=["GET"]),
            Route("/crm/v3/properties/{otype}", self.create_property, methods=["POST"]),
            Route("/crm/v3/properties/{otype}/batch/read", self.batch_read_properties, methods=["POST"]),
            Route("/crm/v3/properties/{otype}/{name}", self.get_property, methods=["GET"]),
            Route("/crm/v3/properties/{otype}/{name}", self.patch_property, methods=["PATCH"]),
            Route("/crm/v3/properties/{otype}/{name}", self.delete_property, methods=["DELETE"]),
            Route("/crm/v3/owners", self.list_owners, methods=["GET"]),
            Route("/crm/v3/owners/", self.list_owners, methods=["GET"]),
            Route("/crm/v3/owners/{oid}", self.get_owner, methods=["GET"]),
            Route("/crm/v3/pipelines/{otype}", self.list_pipelines, methods=["GET"]),
            Route("/crm/v3/pipelines/{otype}/{pid}", self.get_pipeline, methods=["GET"]),
            Route("/crm/v3/pipelines/{otype}/{pid}/stages", self.list_stages, methods=["GET"]),
            Route("/crm/v3/pipelines/{otype}/{pid}/stages/{sid}", self.get_stage, methods=["GET"]),
            Route("/crm/v3/lists", self.lists_method_not_allowed, methods=["GET"]),
            Route("/crm/v3/lists/", self.lists_method_not_allowed, methods=["GET"]),
            Route("/crm/v3/lists", self.create_list, methods=["POST"]),
            Route("/crm/v3/lists/", self.create_list, methods=["POST"]),
            Route("/crm/v3/lists/search", self.search_lists, methods=["POST"]),
            Route("/crm/v3/lists/object-type-id/{tid}/name/{name}", self.get_list_by_name, methods=["GET"]),
            Route("/crm/v3/lists/{lid}", self.get_list, methods=["GET"]),
            Route("/crm/v3/lists/{lid}", self.delete_list, methods=["DELETE"]),
            Route("/crm/v3/lists/{lid}/restore", self.restore_list, methods=["PUT"]),
            Route("/crm/v3/lists/{lid}/update-list-name", self.rename_list, methods=["PUT"]),
            Route("/crm/v3/lists/{lid}/memberships", self.list_memberships, methods=["GET"]),
            Route("/crm/v3/lists/{lid}/memberships/join-order", self.list_memberships, methods=["GET"]),
            Route("/crm/v3/lists/{lid}/memberships/add", self.add_memberships, methods=["PUT"]),
            Route("/crm/v3/lists/{lid}/memberships/remove", self.remove_memberships, methods=["PUT"]),
            Route("/crm/v3/lists/{lid}/memberships/add-and-remove", self.add_and_remove_memberships, methods=["PUT"]),
        ]


def _target_for_type_id(source: ObjectType, type_id: int | None) -> ObjectType | None:
    if type_id is None:
        return None
    for (from_name, to_name), known in _ASSOCIATION_TYPE_IDS.items():
        if known == type_id and from_name == source.name:
            return OBJECT_TYPES[to_name]
    # HubSpot-defined ids for engagements (note→contact 202, note→company 190, note→deal 214, ...).
    by_id = {202: "contacts", 190: "companies", 214: "deals", 216: "tickets", 204: "contacts", 192: "companies"}
    target = by_id.get(type_id)
    return OBJECT_TYPES[target] if target else None


def _bounded_int(raw: object, default: int, maximum: int) -> int:
    if raw is None or raw == "":
        return default
    try:
        value = int(str(raw))
    except ValueError as error:
        raise _validation(f"Invalid limit: {raw}") from error
    if value < 1:
        raise _validation(f"Invalid limit: {raw}")
    return min(value, maximum)


def _offset(raw: object) -> int:
    if raw is None or raw == "":
        return 0
    text = str(raw)
    if not text.isdigit():
        raise _validation(f"Invalid after cursor: {text}")
    return int(text)


def _sorts(raw: object) -> list[tuple[str, bool]]:
    sorts: list[tuple[str, bool]] = []
    for item in cast(list[object], raw) if isinstance(raw, list) else []:
        if isinstance(item, str):
            name = item.lstrip("-")
            sorts.append((name, item.startswith("-")))
        elif isinstance(item, dict):
            spec = cast(dict[str, Any], item)
            name = str(spec.get("propertyName", "")).strip()
            if name:
                sorts.append((name, str(spec.get("direction", "ASCENDING")).upper() == "DESCENDING"))
    return sorts


def make_data_app(store: Store) -> Starlette:
    if not isinstance(store, HubSpotStore):
        raise TypeError("hubspot data app needs a HubSpotStore")
    api = _Api(store)

    async def on_api_error(request: Request, exc: Exception) -> Response:
        assert isinstance(exc, ApiError)
        return api.error_response(request, exc)

    async def on_http_error(request: Request, exc: Exception) -> Response:
        assert isinstance(exc, HTTPException)
        if exc.status_code == 405:
            allow = dict(exc.headers or {}).get("Allow", "")
            error = ApiError(
                405,
                "Method Not Allowed",
                "METHOD_NOT_ALLOWED",
                headers={"allow": allow},
                raw={"detail": "Method Not Allowed"},
            )
        elif exc.status_code == 404:
            error = _not_found()
        else:
            error = ApiError(exc.status_code, str(exc.detail), "INTERNAL_ERROR")
        return api.error_response(request, error)

    return Starlette(
        routes=api.routes(),
        exception_handlers={ApiError: on_api_error, HTTPException: on_http_error},
    )


SPEC = TwinSpec(
    provider="hubspot",
    role="hubspot_crm",
    make_store=HubSpotStore,
    make_data_app=make_data_app,
    notes=(
        "Calibrated against the ArgaBench CRM fixture (real Arga HubSpot twin traffic); "
        "see devsim/calibration/hubspot/NOTES.md.",
        "properties= projection returns requested ∩ defined; undefined stored values only appear in "
        "POST/PATCH/merge responses.",
        "associations in a create body are ignored like the real twin (HubSpotStore.honor_create_associations).",
        "v3 association routes answer 501 endpoint_not_implemented; GET /crm/v3/lists answers 405.",
        "admin state adds objects.<type>.records and a top-level associations list to the real summary shape.",
    ),
)

__all__ = [
    "OBJECT_TYPES",
    "SPEC",
    "ApiError",
    "HubSpotStore",
    "ObjectType",
    "default_association_type_id",
    "make_data_app",
]
