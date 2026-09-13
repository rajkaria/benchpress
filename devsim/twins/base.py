"""Shared machinery for every local twin.

Design rules (shared by every twin):

- Deterministic: ids derive from `sha256(seed_key | collection | ordinal)` in each provider's
  id format; timestamps come from a clock that advances only on writes.
- Reads are pure. Nothing on GET / search may change state (no read counters, no timestamps).
- Real error shapes. Each twin returns its provider's real error envelope and status code.
- Unsafe is possible. Send / charge / delete endpoints exist and work, so a bad agent is
  caught by the grader rather than by a 404.
- Admin plane is separate. `make_admin_app(store)` serves `GET /admin/state` (plus the aliases
  the harness's state capturer knows) from `store.admin_state()`.
"""

from __future__ import annotations

import hashlib
import json
import string
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, cast
from urllib.parse import parse_qsl

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

_ALNUM = string.ascii_letters + string.digits
_UPPER_ALNUM = string.ascii_uppercase + string.digits
_LOWER_ALNUM = string.ascii_lowercase + string.digits

DEFAULT_EPOCH = datetime(2026, 9, 1, 9, 0, 0, tzinfo=UTC)


class Clock:
    """A clock that only moves when a twin writes. Reads never tick it."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or DEFAULT_EPOCH

    def now(self) -> datetime:
        return self._now

    def tick(self, seconds: int = 1) -> datetime:
        self._now = self._now + timedelta(seconds=seconds)
        return self._now

    def epoch(self) -> int:
        return int(self._now.timestamp())

    def epoch_ms(self) -> int:
        return int(self._now.timestamp() * 1000)

    def iso(self) -> str:
        """`2026-09-01T09:00:00Z`"""
        return self._now.strftime("%Y-%m-%dT%H:%M:%SZ")

    def iso_ms(self) -> str:
        """`2026-09-01T09:00:00.000Z` (HubSpot / Linear style)."""
        return self._now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{self._now.microsecond // 1000:03d}Z"

    def slack_ts(self, sequence: int) -> str:
        """Slack message ts: epoch seconds + 6-digit micro-part. Unique per sequence."""
        return f"{self.epoch()}.{sequence % 1_000_000:06d}"


# --------------------------------------------------------------------------------------
# Deterministic identifiers
# --------------------------------------------------------------------------------------


def det_bytes(seed_key: str, *parts: object) -> bytes:
    material = seed_key + "|" + "|".join(str(part) for part in parts)
    return hashlib.sha256(material.encode("utf-8")).digest()


def _det_from_alphabet(seed_key: str, parts: tuple[object, ...], alphabet: str, length: int) -> str:
    out: list[str] = []
    counter = 0
    while len(out) < length:
        digest = det_bytes(seed_key, *parts, counter)
        for byte in digest:
            out.append(alphabet[byte % len(alphabet)])
            if len(out) == length:
                break
        counter += 1
    return "".join(out)


def det_hex(seed_key: str, *parts: object, length: int = 16) -> str:
    return det_bytes(seed_key, *parts).hex()[:length]


def det_digits(seed_key: str, *parts: object, length: int = 10) -> str:
    digits = _det_from_alphabet(seed_key, parts, string.digits, length)
    if digits[0] == "0":
        digits = "7" + digits[1:]
    return digits


def det_alnum(seed_key: str, *parts: object, length: int = 14, case: str = "mixed") -> str:
    alphabet = {"upper": _UPPER_ALNUM, "lower": _LOWER_ALNUM}.get(case, _ALNUM)
    return _det_from_alphabet(seed_key, parts, alphabet, length)


def det_uuid(seed_key: str, *parts: object) -> str:
    raw = det_bytes(seed_key, *parts).hex()
    return f"{raw[0:8]}-{raw[8:12]}-4{raw[13:16]}-a{raw[17:20]}-{raw[20:32]}"


# --------------------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------------------


@dataclass
class Mutation:
    sequence: int
    method: str
    path: str
    collection: str
    record_id: str
    before: object
    after: object
    at: str


class Store:
    """Base class for a twin's in-memory state. Subclasses own their collections."""

    provider: ClassVar[str] = ""

    def __init__(self, seed_key: str, clock: Clock | None = None) -> None:
        self.seed_key = seed_key
        self.clock = clock or Clock()
        self.journal: list[Mutation] = []
        self._ordinals: dict[str, int] = {}

    # -- identifiers -----------------------------------------------------------------

    def next_ordinal(self, collection: str) -> int:
        value = self._ordinals.get(collection, 0) + 1
        self._ordinals[collection] = value
        return value

    def new_id(
        self, collection: str, *, kind: str = "hex", length: int = 16, prefix: str = "", case: str = "mixed"
    ) -> str:
        ordinal = self.next_ordinal(collection)
        if kind == "hex":
            body = det_hex(self.seed_key, collection, ordinal, length=length)
        elif kind == "digits":
            body = det_digits(self.seed_key, collection, ordinal, length=length)
        elif kind == "uuid":
            body = det_uuid(self.seed_key, collection, ordinal)
        else:
            body = det_alnum(self.seed_key, collection, ordinal, length=length, case=case)
        return f"{prefix}{body}"

    # -- mutations -------------------------------------------------------------------

    def record_mutation(
        self,
        *,
        method: str,
        path: str,
        collection: str,
        record_id: str,
        before: object,
        after: object,
    ) -> Mutation:
        """Append to the journal and advance the clock. Call this from every write route."""
        self.clock.tick()
        mutation = Mutation(
            sequence=len(self.journal) + 1,
            method=method.upper(),
            path=path,
            collection=collection,
            record_id=record_id,
            before=_copy_json(before),
            after=_copy_json(after),
            at=self.clock.iso(),
        )
        self.journal.append(mutation)
        return mutation

    # -- contract every twin implements ----------------------------------------------

    def seed(self, seed_config: Mapping[str, Any]) -> None:
        """Load this provider's slice of a scenario `seed_config` (the published JSON)."""
        raise NotImplementedError

    def admin_state(self) -> dict[str, Any]:
        """The JSON object served at `/admin/state`. Must be a dict; must be pure."""
        raise NotImplementedError


# --------------------------------------------------------------------------------------
# HTTP helpers shared by data-plane apps
# --------------------------------------------------------------------------------------


def json_response(payload: object, status: int = 200, headers: Mapping[str, str] | None = None) -> JSONResponse:
    return JSONResponse(_copy_json(payload), status_code=status, headers=dict(headers or {}))


def decode_form(pairs: Sequence[tuple[str, str]]) -> dict[str, Any]:
    """Decode `a=1&b[c]=2&items[0][price]=x` into nested dicts/lists (Stripe / Slack style)."""
    root: dict[str, Any] = {}
    for raw_key, value in pairs:
        keys = _bracket_keys(raw_key)
        _assign(root, keys, value)
    return _listify(root)


def _bracket_keys(raw_key: str) -> list[str]:
    head, _, rest = raw_key.partition("[")
    keys = [head]
    while rest:
        segment, _, rest = rest.partition("]")
        keys.append(segment)
        if rest.startswith("["):
            rest = rest[1:]
    return keys


def _assign(node: dict[str, Any], keys: list[str], value: str) -> None:
    for key in keys[:-1]:
        child = node.get(key)
        if not isinstance(child, dict):
            child = {}
            node[key] = child
        node = cast(dict[str, Any], child)
    node[keys[-1]] = value


def _listify(node: object) -> Any:
    if isinstance(node, dict):
        typed = cast(dict[str, Any], node)
        converted = {key: _listify(value) for key, value in typed.items()}
        if converted and all(key.isdigit() for key in converted):
            return [converted[key] for key in sorted(converted, key=int)]
        return converted
    return node


async def parse_body(request: Request) -> tuple[dict[str, Any], str]:
    """Return `(body, encoding)` where encoding is `json`, `form` or `none`."""
    raw = await request.body()
    if not raw:
        return {}, "none"
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type == "application/x-www-form-urlencoded":
        return decode_form(parse_qsl(raw.decode("utf-8", errors="replace"), keep_blank_values=True)), "form"
    try:
        parsed: object = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        if b"=" in raw and b"{" not in raw:
            return decode_form(parse_qsl(raw.decode("utf-8", errors="replace"), keep_blank_values=True)), "form"
        return {}, "invalid"
    if isinstance(parsed, dict):
        return cast(dict[str, Any], parsed), "json"
    return {"_value": parsed}, "json"


def query_params(request: Request) -> dict[str, Any]:
    """Query string with bracket notation decoded (`created[gte]=…`, `expand[]=…`)."""
    return decode_form(list(request.query_params.multi_items()))


def _copy_json(value: object) -> Any:
    return json.loads(json.dumps(value, default=str))


# --------------------------------------------------------------------------------------
# Admin plane
# --------------------------------------------------------------------------------------

ADMIN_STATE_PATHS: tuple[str, ...] = ("/admin/state", "/_admin/state", "/inspect")


def make_admin_app(store: Store, *, paths: Sequence[str] = ADMIN_STATE_PATHS) -> Starlette:
    """Serve `store.admin_state()` at every admin alias, plus a health route."""

    async def state(_: Request) -> Response:
        return json_response(store.admin_state())

    async def health(_: Request) -> Response:
        return json_response({"ok": True, "provider": store.provider})

    async def journal(_: Request) -> Response:
        return json_response([mutation.__dict__ for mutation in store.journal])

    routes = [Route(path, state, methods=["GET"]) for path in paths]
    routes.append(Route("/healthz", health, methods=["GET"]))
    routes.append(Route("/__devsim/journal", journal, methods=["GET"]))
    return Starlette(routes=routes)


# --------------------------------------------------------------------------------------
# Spec
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TwinSpec:
    """What the scaffold needs to host one provider twin."""

    provider: str
    role: str
    make_store: Callable[[str], Store]
    make_data_app: Callable[[Store], Starlette]
    make_admin_app: Callable[[Store], Starlette] = make_admin_app
    admin_paths: tuple[str, ...] = ADMIN_STATE_PATHS
    notes: tuple[str, ...] = field(default_factory=tuple)
