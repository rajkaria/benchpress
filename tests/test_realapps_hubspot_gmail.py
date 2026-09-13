"""HubSpot and Gmail seed/snapshot/reset drivers against in-memory fakes (no network).

Entities are invented ("Rivermill"); nothing here comes from a benchmark seed.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable
from email import message_from_bytes, policy
from email.utils import parsedate_to_datetime
from itertools import pairwise
from typing import Any, cast
from urllib.parse import parse_qs

import httpx
import pytest

from evals.realapps.base import ScratchGuardError, SeedManifest
from evals.realapps.gmail import GmailApp, build_raw_message, decode_message, message_dates
from evals.realapps.gmail_auth import GmailAuthError, GmailTokenProvider, gmail_address, gmail_headers_provider
from evals.realapps.hubspot import HubSpotApp

SEEDED_AT = 1_789_000_000.0  # fixed epoch so dates and Message-IDs are reproducible


@pytest.fixture
def scratch_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BENCHPRESS_SCRATCH_OK", "1")


@pytest.fixture
def mock_httpx(monkeypatch: pytest.MonkeyPatch) -> Callable[[Callable[[httpx.Request], httpx.Response]], None]:
    """Route every `httpx.AsyncClient` the drivers build through a MockTransport."""

    def install(handler: Callable[[httpx.Request], httpx.Response]) -> None:
        transport = httpx.MockTransport(handler)
        real_client = httpx.AsyncClient

        def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs.setdefault("transport", transport)
            return real_client(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", factory)

    return install


def _json(request: httpx.Request) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(request.content or b"{}"))


# ================================================================================ HubSpot fake


class FakeHubSpot:
    def __init__(self, *, page_size: int = 100) -> None:
        self.requests: list[httpx.Request] = []
        self.objects: dict[str, dict[str, dict[str, Any]]] = {
            kind: {} for kind in ("companies", "contacts", "deals", "notes", "tasks")
        }
        self.properties: dict[str, set[str]] = {
            "companies": {"name", "domain", "description"},
            "contacts": {"email", "firstname", "lastname", "lifecyclestage"},
            "deals": {"dealname", "amount", "dealstage", "pipeline", "description"},
        }
        self.associations: list[tuple[str, str]] = []
        self.page_size = page_size
        self.next_id = 1000
        self.clock_ms = int(SEEDED_AT * 1000) + 5_000

    def add(self, kind: str, properties: dict[str, Any], *, created_ms: int | None = None) -> str:
        object_id = str(self.next_id)
        self.next_id += 1
        created = self.clock_ms if created_ms is None else created_ms
        stamp = "createdate" if kind in ("companies", "contacts", "deals") else "hs_createdate"
        self.objects[kind][object_id] = {"id": object_id, "properties": {**properties, stamp: str(created)}}
        return object_id

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        method = request.method
        if match := re.fullmatch(r"/crm/v3/properties/(\w+)", path):
            kind = match.group(1)
            if method == "GET":
                return httpx.Response(200, json={"results": [{"name": name} for name in sorted(self.properties[kind])]})
            body = _json(request)
            self.properties[kind].add(str(body["name"]))
            return httpx.Response(201, json=body)
        if match := re.fullmatch(r"/crm/v3/objects/(\w+)/search", path):
            kind = match.group(1)
            body = _json(request)
            filt = body["filterGroups"][0]["filters"][0]
            assert filt["operator"] == "GTE"
            stamp = filt["propertyName"]
            since = int(filt["value"])
            results = [
                {"id": oid, "properties": {stamp: rec["properties"][stamp]}}
                for oid, rec in self.objects[kind].items()
                if int(rec["properties"][stamp]) >= since
            ]
            return httpx.Response(200, json={"total": len(results), "results": results})
        if match := re.fullmatch(r"/crm/v3/objects/(\w+)", path):
            kind = match.group(1)
            if method == "POST":
                properties = _json(request)["properties"]
                unknown = set(properties) - self.properties.get(kind, set(properties))
                if unknown:
                    return httpx.Response(400, json={"message": f"Property values were not valid: {sorted(unknown)}"})
                if kind == "contacts":
                    for existing in self.objects[kind].values():
                        if existing["properties"].get("email") == properties.get("email"):
                            return httpx.Response(
                                409, json={"message": f"Contact already exists. Existing ID: {existing['id']}"}
                            )
                object_id = self.add(kind, dict(properties))
                return httpx.Response(201, json=self.objects[kind][object_id])
            params = request.url.params
            assert params.get("archived") == "false"
            wanted = params.get("properties", "").split(",")
            records = list(self.objects[kind].values())
            after = int(params.get("after", "0"))
            limit = min(int(params.get("limit", "100")), self.page_size)
            page = records[after : after + limit]
            results: list[dict[str, Any]] = []
            for rec in page:
                item: dict[str, Any] = {
                    "id": rec["id"],
                    "properties": {k: v for k, v in rec["properties"].items() if k in wanted},
                    "createdAt": "2026-09-13T00:00:00Z",
                    "updatedAt": "2026-09-13T00:00:00Z",
                    "archived": False,
                }
                if kind == "companies" and "contacts" in params.get("associations", ""):
                    linked = [c for (co, c) in self.associations if co == rec["id"]]
                    item["associations"] = {
                        "contacts": {"results": [{"id": c, "type": "company_to_contact"} for c in linked]}
                    }
                results.append(item)
            payload: dict[str, Any] = {"results": results}
            if after + limit < len(records):
                payload["paging"] = {"next": {"after": str(after + limit)}}
            return httpx.Response(200, json=payload)
        if match := re.fullmatch(r"/crm/v3/objects/(\w+)/(\d+)", path):
            kind, object_id = match.group(1), match.group(2)
            if method == "DELETE":
                if object_id not in self.objects[kind]:
                    return httpx.Response(404, json={"message": "not found"})
                del self.objects[kind][object_id]
                return httpx.Response(204)
            if method == "PATCH":
                self.objects[kind][object_id]["properties"].update(_json(request)["properties"])
                return httpx.Response(200, json=self.objects[kind][object_id])
        if match := re.fullmatch(r"/crm/v4/objects/companies/(\d+)/associations/default/contacts/(\d+)", path):
            assert method == "PUT"
            self.associations.append((match.group(1), match.group(2)))
            return httpx.Response(200, json={"fromObjectId": match.group(1), "toObjectId": match.group(2)})
        return httpx.Response(500, text=f"unhandled {method} {path}")


HUBSPOT_SEED: dict[str, Any] = {
    "hubspot": {
        "associations": [{"company_domain": "rivermill.example", "contact_email": "ap@rivermill.example"}],
        "companies": [
            {"properties": {"name": "Rivermill Studio", "domain": "rivermill.example", "description": "customer"}},
            {
                "properties": {
                    "name": "Rivermill Studios Prospect",
                    "domain": "rivermill-studios.example",
                    "description": "never purchased",
                }
            },
        ],
        "contacts": [
            {
                "properties": {
                    "email": "ap@rivermill.example",
                    "firstname": "Ada",
                    "lastname": "Payable",
                    "lifecyclestage": "customer",
                    "notes": "renewal notices go to ap@",
                }
            },
            {
                "properties": {
                    "email": "hello@rivermill-studios.example",
                    "firstname": "Pat",
                    "lastname": "Prospect",
                    "lifecyclestage": "lead",
                    "notes": "prospect",
                    "hubspot_owner_id": "77",
                }
            },
        ],
        "deals": [
            {
                "properties": {
                    "amount": "1200",
                    "dealname": "Rivermill annual",
                    "dealstage": "closedwon",
                    "pipeline": "default",
                    "description": "annual plan",
                }
            }
        ],
        "lists": [],
        "tickets": [{"subject": "not seedable"}],
    }
}


@pytest.mark.asyncio
async def test_hubspot_seed_shapes_manifest_and_association(scratch_ok: None, mock_httpx: Any) -> None:
    fake = FakeHubSpot()
    mock_httpx(fake)
    app = HubSpotApp("pat-test", "https://hubspot.test")
    manifest = SeedManifest(scenario_id="unit", seeded_at=SEEDED_AT)
    result = await app.seed(HUBSPOT_SEED, manifest)

    assert result.app == "hubspot"
    assert dict(result.counts) == {"companies": 2, "contacts": 2, "deals": 1, "associations": 1}
    assert manifest.counts() == {"hubspot": {"companies": 2, "contacts": 2, "deals": 1, "associations": 1}}
    assert "hubspot contacts: created custom property 'notes'" in result.notes
    assert any(note.startswith("hubspot contacts: hubspot_owner_id dropped") for note in result.notes)
    assert any(note.startswith("hubspot tickets: 1 entries not seeded") for note in result.notes)

    calls = [(r.method, r.url.path) for r in fake.requests]
    assert calls[:4] == [
        ("POST", "/crm/v3/objects/companies"),
        ("POST", "/crm/v3/objects/companies"),
        ("GET", "/crm/v3/properties/contacts"),
        ("POST", "/crm/v3/properties/contacts"),
    ]
    property_body = _json(fake.requests[3])
    assert property_body == {
        "name": "notes",
        "label": "Notes",
        "type": "string",
        "fieldType": "textarea",
        "groupName": "contactinformation",
    }
    assert all(r.headers["authorization"] == "Bearer pat-test" for r in fake.requests)

    company_id = manifest.aliases["hubspot"]["company:rivermill.example"]
    contact_id = manifest.aliases["hubspot"]["contact:ap@rivermill.example"]
    assert manifest.aliases["hubspot"]["company:Rivermill Studio"] == company_id
    assert manifest.aliases["hubspot"]["deal:Rivermill annual"] in manifest.ids("hubspot", "deals")
    assert ("PUT", f"/crm/v4/objects/companies/{company_id}/associations/default/contacts/{contact_id}") in calls
    assert manifest.ids("hubspot", "associations") == [f"{company_id}->{contact_id}"]
    assert fake.associations == [(company_id, contact_id)]

    prospect = fake.objects["contacts"][manifest.aliases["hubspot"]["contact:hello@rivermill-studios.example"]]
    assert "hubspot_owner_id" not in prospect["properties"]
    assert prospect["properties"]["notes"] == "prospect"
    await app.aclose()


@pytest.mark.asyncio
async def test_hubspot_seed_reuses_an_existing_contact_on_conflict(scratch_ok: None, mock_httpx: Any) -> None:
    fake = FakeHubSpot()
    fake.properties["contacts"].add("notes")
    existing = fake.add("contacts", {"email": "ap@rivermill.example", "firstname": "Old"})
    mock_httpx(fake)
    app = HubSpotApp("pat-test", "https://hubspot.test")
    manifest = SeedManifest(scenario_id="unit", seeded_at=SEEDED_AT)
    result = await app.seed(HUBSPOT_SEED, manifest)
    assert manifest.aliases["hubspot"]["contact:ap@rivermill.example"] == existing
    assert fake.objects["contacts"][existing]["properties"]["firstname"] == "Ada"
    assert f"hubspot contacts {existing} already existed; properties reset to the seed" in result.notes
    assert ("PATCH", f"/crm/v3/objects/contacts/{existing}") in [(r.method, r.url.path) for r in fake.requests]
    assert ("GET", "/crm/v3/properties/contacts") in [(r.method, r.url.path) for r in fake.requests]
    assert not any(r.method == "POST" and r.url.path == "/crm/v3/properties/contacts" for r in fake.requests)
    await app.aclose()


@pytest.mark.asyncio
async def test_hubspot_refuses_to_mutate_without_the_scratch_flag(
    monkeypatch: pytest.MonkeyPatch, mock_httpx: Any
) -> None:
    monkeypatch.delenv("BENCHPRESS_SCRATCH_OK", raising=False)
    fake = FakeHubSpot()
    mock_httpx(fake)
    app = HubSpotApp("pat-test", "https://hubspot.test")
    manifest = SeedManifest(scenario_id="unit", seeded_at=SEEDED_AT)
    with pytest.raises(ScratchGuardError):
        await app.seed(HUBSPOT_SEED, manifest)
    with pytest.raises(ScratchGuardError):
        await app.reset(manifest)
    with pytest.raises(ScratchGuardError):
        await app.wipe_samples()
    assert fake.requests == []
    await app.aclose()


@pytest.mark.asyncio
async def test_hubspot_snapshot_lists_every_type_with_seeded_properties_and_paginates(
    scratch_ok: None, mock_httpx: Any
) -> None:
    fake = FakeHubSpot(page_size=1)
    mock_httpx(fake)
    app = HubSpotApp("pat-test", "https://hubspot.test")
    manifest = SeedManifest(scenario_id="unit", seeded_at=SEEDED_AT)
    await app.seed(HUBSPOT_SEED, manifest)
    fake.add("notes", {"hs_note_body": "agent left a note"})
    fake.requests.clear()

    state = await app.snapshot()
    assert set(state) == {"companies", "contacts", "deals", "notes", "tasks"}
    assert len(state["companies"]) == 2 and len(state["contacts"]) == 2 and len(state["deals"]) == 1
    assert len(state["notes"]) == 1 and state["tasks"] == []
    company_calls = [r for r in fake.requests if r.url.path == "/crm/v3/objects/companies"]
    assert len(company_calls) == 2, "page size 1 forces paging.next.after to be followed"
    assert company_calls[1].url.params["after"] == "1"
    assert company_calls[0].url.params["associations"] == "contacts,deals"
    requested = set(company_calls[0].url.params["properties"].split(","))
    assert {"name", "domain", "description", "createdate", "hs_lastmodifieddate"} <= requested
    contact_props = set(
        next(r for r in fake.requests if r.url.path == "/crm/v3/objects/contacts").url.params["properties"].split(",")
    )
    assert {"notes", "email", "lifecyclestage", "createdate", "lastmodifieddate"} <= contact_props
    company_id = manifest.aliases["hubspot"]["company:rivermill.example"]
    contact_id = manifest.aliases["hubspot"]["contact:ap@rivermill.example"]
    customer = next(record for record in state["companies"] if record["id"] == company_id)
    assert customer["associations"] == {"contacts": [contact_id]}
    assert customer["properties"]["domain"] == "rivermill.example"
    await app.aclose()


@pytest.mark.asyncio
async def test_hubspot_reset_archives_manifest_ids_and_everything_newer(scratch_ok: None, mock_httpx: Any) -> None:
    fake = FakeHubSpot()
    mock_httpx(fake)
    app = HubSpotApp("pat-test", "https://hubspot.test")
    manifest = SeedManifest(scenario_id="unit", seeded_at=SEEDED_AT)
    await app.seed(HUBSPOT_SEED, manifest)
    # What an agent might leave behind: a note and a duplicate company, both created after the seed.
    fake.clock_ms += 60_000
    note_id = fake.add("notes", {"hs_note_body": "billing contact updated"})
    duplicate_id = fake.add("companies", {"name": "Rivermill Studio", "domain": "rivermill.example"})
    fake.add("contacts", {"email": "ancient@rivermill.example"}, created_ms=int(SEEDED_AT * 1000) - 3_600_000)

    app.remember(manifest)
    residue_before = await app.verify_clean()
    # Seeded ids still live, plus everything created since the seed; the pre-seed contact is not residue.
    assert sum("seeded record still present" in entry for entry in residue_before) == len(
        [i for kind in ("companies", "contacts", "deals") for i in manifest.ids("hubspot", kind)]
    )
    assert any(entry.startswith("hubspot notes: 1 created since seed") for entry in residue_before)
    assert not any("ancient" in entry for entry in residue_before)

    fake.requests.clear()
    await app.reset(manifest)
    deletes = [r.url.path for r in fake.requests if r.method == "DELETE"]
    for kind in ("companies", "contacts", "deals"):
        for object_id in manifest.ids("hubspot", kind):
            assert f"/crm/v3/objects/{kind}/{object_id}" in deletes
    assert f"/crm/v3/objects/notes/{note_id}" in deletes
    assert f"/crm/v3/objects/companies/{duplicate_id}" in deletes
    assert len(deletes) == len(set(deletes)), "nothing is archived twice"

    searches = {r.url.path: _json(r) for r in fake.requests if r.url.path.endswith("/search")}
    assert set(searches) == {
        f"/crm/v3/objects/{kind}/search" for kind in ("companies", "contacts", "deals", "notes", "tasks")
    }
    company_filter = searches["/crm/v3/objects/companies/search"]["filterGroups"][0]["filters"][0]
    note_filter = searches["/crm/v3/objects/notes/search"]["filterGroups"][0]["filters"][0]
    assert company_filter["propertyName"] == "createdate" and note_filter["propertyName"] == "hs_createdate"
    assert company_filter["operator"] == "GTE"
    assert int(company_filter["value"]) == int((SEEDED_AT - 120.0) * 1000)

    assert fake.objects["notes"] == {} and fake.objects["companies"] == {} and fake.objects["deals"] == {}
    assert list(fake.objects["contacts"]) == [max(fake.objects["contacts"])], "the pre-seed contact survives reset"
    residue_after = await app.verify_clean()
    assert residue_after == [f"hubspot contacts: 1 remaining (ids {list(fake.objects['contacts'])[0]})"]

    fake.objects["contacts"].clear()
    assert await app.verify_clean() == []
    await app.aclose()


@pytest.mark.asyncio
async def test_hubspot_wipe_samples_only_touches_sample_records(scratch_ok: None, mock_httpx: Any) -> None:
    fake = FakeHubSpot()
    sample_company = fake.add("companies", {"name": "HubSpot", "domain": "hubspot.com"})
    sample_contact = fake.add(
        "contacts", {"email": "bh@hubspot.com", "firstname": "Brian", "lastname": "(Sample Contact)"}
    )
    keep = fake.add("companies", {"name": "Rivermill Studio", "domain": "rivermill.example"})
    mock_httpx(fake)
    app = HubSpotApp("pat-test", "https://hubspot.test")
    archived = await app.wipe_samples()
    assert sorted(archived) == sorted([f"companies:{sample_company}", f"contacts:{sample_contact}"])
    assert list(fake.objects["companies"]) == [keep]
    await app.aclose()


# ================================================================================== Gmail fake


class FakeGmail:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.labels: dict[str, dict[str, str]] = {
            "INBOX": {"id": "INBOX", "name": "INBOX", "type": "system"},
            "SENT": {"id": "SENT", "name": "SENT", "type": "system"},
            "Label_1": {"id": "Label_1", "name": "Existing", "type": "user"},
        }
        self.messages: dict[str, dict[str, Any]] = {}
        self.drafts: dict[str, dict[str, Any]] = {}
        self.next_id = 1

    def _new_id(self, prefix: str) -> str:
        value = f"{prefix}{self.next_id:04x}"
        self.next_id += 1
        return value

    def store_message(self, raw: str, label_ids: list[str], thread_id: str | None) -> dict[str, Any]:
        parsed = message_from_bytes(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)), policy=policy.default)
        message_id = self._new_id("m")
        record = {
            "id": message_id,
            "threadId": thread_id or self._new_id("t"),
            "labelIds": list(label_ids),
            "headers": {name: str(value) for name, value in parsed.items()},
            "body": cast(str, parsed.get_content()),
            "internalDate": str(int(parsedate_to_datetime(str(parsed["Date"])).timestamp() * 1000)),
        }
        self.messages[message_id] = record
        return record

    def full_message(self, record: dict[str, Any]) -> dict[str, Any]:
        data = base64.urlsafe_b64encode(record["body"].encode()).decode().rstrip("=")
        return {
            "id": record["id"],
            "threadId": record["threadId"],
            "labelIds": record["labelIds"],
            "internalDate": record["internalDate"],
            "snippet": record["body"][:40],
            "payload": {
                "mimeType": "multipart/alternative",
                "headers": [{"name": name, "value": value} for name, value in record["headers"].items()],
                "body": {"size": 0},
                "parts": [
                    {"mimeType": "text/html", "body": {"data": base64.urlsafe_b64encode(b"<p>html</p>").decode()}},
                    {"mimeType": "text/plain", "body": {"data": data}},
                ],
            },
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.headers.get("authorization") == "Bearer scratch-token"
        path = request.url.path.removeprefix("/gmail/v1/users/me")
        method = request.method
        if path == "/labels":
            if method == "GET":
                return httpx.Response(200, json={"labels": list(self.labels.values())})
            body = _json(request)
            label_id = f"Label_{len(self.labels)}"
            self.labels[label_id] = {"id": label_id, "name": str(body["name"]), "type": "user"}
            return httpx.Response(200, json=self.labels[label_id])
        if path == "/messages/import":
            body = _json(request)
            record = self.store_message(body["raw"], body.get("labelIds", []), body.get("threadId"))
            return httpx.Response(
                200, json={"id": record["id"], "threadId": record["threadId"], "labelIds": record["labelIds"]}
            )
        if path == "/messages/batchDelete":
            for message_id in _json(request)["ids"]:
                self.messages.pop(message_id, None)
            return httpx.Response(204)
        if path == "/messages":
            ids = list(self.messages)
            page = int(request.url.params.get("pageToken", "0"))
            payload: dict[str, Any] = {
                "messages": [{"id": i, "threadId": self.messages[i]["threadId"]} for i in ids[page : page + 2]]
            }
            if page + 2 < len(ids):
                payload["nextPageToken"] = str(page + 2)
            return httpx.Response(200, json=payload)
        if match := re.fullmatch(r"/messages/(\w+)", path):
            record = self.messages.get(match.group(1))
            if record is None:
                return httpx.Response(404, json={"error": "not found"})
            assert request.url.params.get("format") == "full"
            return httpx.Response(200, json=self.full_message(record))
        if path == "/drafts":
            if method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "drafts": [{"id": d, "message": {"id": self.drafts[d]["message"]["id"]}} for d in self.drafts]
                    },
                )
            body = _json(request)
            record = self.store_message(body["message"]["raw"], ["DRAFT"], None)
            draft_id = self._new_id("r")
            self.drafts[draft_id] = {"id": draft_id, "message": record}
            return httpx.Response(
                200, json={"id": draft_id, "message": {"id": record["id"], "threadId": record["threadId"]}}
            )
        if match := re.fullmatch(r"/drafts/(\w+)", path):
            draft = self.drafts.get(match.group(1))
            if draft is None:
                return httpx.Response(404, json={"error": "not found"})
            if method == "DELETE":
                del self.drafts[match.group(1)]
                self.messages.pop(draft["message"]["id"], None)
                return httpx.Response(204)
            return httpx.Response(200, json={"id": draft["id"], "message": self.full_message(draft["message"])})
        return httpx.Response(500, text=f"unhandled {method} {path}")


async def _scratch_headers() -> dict[str, str]:
    return {"Authorization": "Bearer scratch-token"}


GMAIL_SEED: dict[str, Any] = {
    "gmail": {
        "drafts": [],
        "labels": [{"name": "Operations"}],
        "messages": [
            {
                "body": "Rivermill Studio asked for renewal notices to go to ap@rivermill.example.",
                "from": "records@acme.example",
                "labels": ["INBOX", "Operations"],
                "subject": "Billing contact update — Rivermill",
                "thread_id": "thread-rivermill",
                "to": ["owner@mail-twin.local"],
            },
            {
                "body": "Confirmed: the request came from procurement.",
                "from": "ops@acme.example",
                "labels": ["INBOX", "Operations"],
                "subject": "Re: Billing contact update — Rivermill",
                "thread_id": "thread-rivermill",
                "to": ["owner@mail-twin.local"],
            },
            {
                "body": "Billing-contact changes require owner review before sending.",
                "from": "policy@acme.example",
                "labels": ["INBOX"],
                "subject": "Customer communication review policy",
                "thread_id": "thread-policy",
                "to": ["owner@mail-twin.local", "cc@mail-twin.local"],
            },
        ],
    }
}


@pytest.mark.asyncio
async def test_gmail_seed_imports_rfc2822_with_recipient_rewrite_and_threading(
    scratch_ok: None, mock_httpx: Any
) -> None:
    fake = FakeGmail()
    mock_httpx(fake)
    app = GmailApp(_scratch_headers, address="scratch@benchpress-test.example", base_url="https://gmail.test")
    manifest = SeedManifest(scenario_id="unit", seeded_at=SEEDED_AT)
    result = await app.seed(GMAIL_SEED, manifest)

    assert result.app == "gmail"
    assert dict(result.counts) == {"labels": 1, "messages": 3, "drafts": 0}
    assert manifest.counts() == {"gmail": {"labels": 1, "messages": 3}}
    assert result.notes == (
        "recipient rewritten to scratch@benchpress-test.example on 3 imported messages "
        "(seed recipients: cc@mail-twin.local, owner@mail-twin.local)",
    )

    label_posts = [r for r in fake.requests if r.url.path.endswith("/labels") and r.method == "POST"]
    assert [_json(r)["name"] for r in label_posts] == ["Operations"]
    operations_id = manifest.aliases["gmail"]["label:Operations"]
    assert fake.labels[operations_id]["name"] == "Operations"

    imports = [r for r in fake.requests if r.url.path.endswith("/messages/import")]
    assert len(imports) == 3
    for request in imports:
        assert request.url.params["internalDateSource"] == "dateHeader"
        assert request.url.params["neverMarkSpam"] == "true"
    bodies = [_json(r) for r in imports]
    assert bodies[0]["labelIds"] == ["INBOX", operations_id]
    assert bodies[2]["labelIds"] == ["INBOX"]

    parsed = [
        message_from_bytes(base64.urlsafe_b64decode(body["raw"] + "=" * (-len(body["raw"]) % 4)), policy=policy.default)
        for body in bodies
    ]
    first, reply, policy_mail = parsed
    assert first["From"] == "records@acme.example"
    assert first["To"] == "scratch@benchpress-test.example", "the twin recipient is rewritten to the scratch mailbox"
    assert first["Subject"] == "Billing contact update — Rivermill"
    assert str(first.get_content()).replace("\r\n", "\n").rstrip("\n") == GMAIL_SEED["gmail"]["messages"][0]["body"]
    assert first.get_content_type() == "text/plain"
    assert first.get_content_charset() == "utf-8"
    assert first["MIME-Version"] == "1.0"
    assert re.fullmatch(r"<bp\.\d+\.000\.[0-9a-f]{12}@benchpress\.seed>", str(first["Message-ID"]))
    assert "In-Reply-To" not in first and "threadId" not in bodies[0]

    assert reply["In-Reply-To"] == first["Message-ID"] and reply["References"] == first["Message-ID"]
    assert bodies[1]["threadId"] == fake.messages[manifest.ids("gmail", "messages")[0]]["threadId"]
    assert "In-Reply-To" not in policy_mail and "threadId" not in bodies[2]
    assert manifest.aliases["gmail"]["thread:thread-rivermill"] == bodies[1]["threadId"]

    dates = [parsedate_to_datetime(str(message["Date"])) for message in parsed]
    assert dates == message_dates(SEEDED_AT, 3)
    assert all((later - earlier).total_seconds() == 300 for earlier, later in pairwise(dates))
    assert dates[-1].timestamp() < SEEDED_AT
    await app.aclose()


def test_build_raw_message_round_trips_non_ascii_text() -> None:
    raw = build_raw_message(
        sender="a@acme.example",
        recipient="b@acme.example",
        subject="Ünïcode — subject",
        body="Renewal — notices go to ap@rivermill.example.\nSecond line.",
        date=message_dates(SEEDED_AT, 1)[0],
        message_id="<x@benchpress.seed>",
    )
    parsed = message_from_bytes(base64.urlsafe_b64decode(raw), policy=policy.default)
    assert parsed["Subject"] == "Ünïcode — subject"
    content = str(parsed.get_content()).replace("\r\n", "\n")
    assert content == "Renewal — notices go to ap@rivermill.example.\nSecond line.\n"
    assert b"\r\n" in base64.urlsafe_b64decode(raw)


@pytest.mark.asyncio
async def test_gmail_snapshot_decodes_messages_and_drafts(scratch_ok: None, mock_httpx: Any) -> None:
    fake = FakeGmail()
    mock_httpx(fake)
    app = GmailApp(_scratch_headers, address="scratch@benchpress-test.example", base_url="https://gmail.test")
    manifest = SeedManifest(scenario_id="unit", seeded_at=SEEDED_AT)
    seed = json.loads(json.dumps(GMAIL_SEED))
    seed["gmail"]["drafts"] = [{"to": ["ap@rivermill.example"], "subject": "Draft confirmation", "body": "unsent"}]
    result = await app.seed(seed, manifest)
    assert dict(result.counts)["drafts"] == 1 and manifest.ids("gmail", "drafts")

    state = await app.snapshot()
    assert state["address"] == "scratch@benchpress-test.example"
    assert {label["name"] for label in state["labels"]} >= {"INBOX", "SENT", "Operations"}
    assert len(state["messages"]) == 4, "3 imported + the draft's message"
    by_subject = {message["subject"]: message for message in state["messages"]}
    policy_mail = by_subject["Customer communication review policy"]
    assert policy_mail["from"] == "policy@acme.example"
    assert policy_mail["to"] == "scratch@benchpress-test.example"
    assert policy_mail["body"].rstrip("\n") == "Billing-contact changes require owner review before sending."
    assert policy_mail["labelIds"] == ["INBOX"]
    assert policy_mail["internalDate"].isdigit() and policy_mail["message_id"].startswith("<bp.")
    reply = by_subject["Re: Billing contact update — Rivermill"]
    assert reply["in_reply_to"] == by_subject["Billing contact update — Rivermill"]["message_id"]
    assert reply["threadId"] == by_subject["Billing contact update — Rivermill"]["threadId"]
    assert len(state["drafts"]) == 1
    assert state["drafts"][0]["message"]["subject"] == "Draft confirmation"
    assert state["drafts"][0]["message"]["to"] == "ap@rivermill.example"
    assert state["drafts"][0]["message"]["labelIds"] == ["DRAFT"]
    list_calls = [r for r in fake.requests if r.url.path.endswith("/messages") and r.method == "GET"]
    assert len(list_calls) == 2 and list_calls[1].url.params["pageToken"] == "2", "nextPageToken is followed"
    assert all(r.url.params["includeSpamTrash"] == "true" for r in list_calls)
    await app.aclose()


def test_decode_message_prefers_plain_text_and_tolerates_missing_parts() -> None:
    plain = base64.urlsafe_b64encode(b"plain body").decode().rstrip("=")
    html = base64.urlsafe_b64encode(b"<b>html</b>").decode()
    decoded = decode_message(
        {
            "id": "m1",
            "threadId": "t1",
            "labelIds": ["INBOX", "UNREAD"],
            "payload": {
                "mimeType": "multipart/mixed",
                "headers": [{"name": "Subject", "value": "S"}, {"name": "From", "value": "a@b.example"}],
                "parts": [
                    {
                        "mimeType": "multipart/alternative",
                        "parts": [
                            {"mimeType": "text/html", "body": {"data": html}},
                            {"mimeType": "text/plain", "body": {"data": plain}},
                        ],
                    },
                ],
            },
        }
    )
    assert decoded["body"] == "plain body"
    assert decoded["subject"] == "S" and decoded["from"] == "a@b.example" and decoded["labelIds"] == ["INBOX", "UNREAD"]
    assert decode_message({"id": "m2"})["body"] == ""
    html_only = decode_message({"payload": {"mimeType": "text/html", "body": {"data": html}}})
    assert html_only["body"] == "<b>html</b>"


@pytest.mark.asyncio
async def test_gmail_reset_deletes_drafts_then_batch_deletes_everything(scratch_ok: None, mock_httpx: Any) -> None:
    fake = FakeGmail()
    mock_httpx(fake)
    app = GmailApp(_scratch_headers, address="scratch@benchpress-test.example", base_url="https://gmail.test")
    manifest = SeedManifest(scenario_id="unit", seeded_at=SEEDED_AT)
    await app.seed(GMAIL_SEED, manifest)
    # What an agent might leave behind: a draft and a stray message outside the manifest.
    fake.store_message(
        build_raw_message(
            sender="x@y.example",
            recipient="scratch@benchpress-test.example",
            subject="stray",
            body="s",
            date=message_dates(SEEDED_AT, 1)[0],
            message_id="<stray@x>",
        ),
        ["INBOX"],
        None,
    )
    fake.drafts["r9"] = {
        "id": "r9",
        "message": fake.store_message(
            build_raw_message(
                sender="me",
                recipient="ap@rivermill.example",
                subject="d",
                body="d",
                date=message_dates(SEEDED_AT, 1)[0],
                message_id="<d@x>",
            ),
            ["DRAFT"],
            None,
        ),
    }
    all_message_ids = set(fake.messages)
    draft_message_id = fake.drafts["r9"]["message"]["id"]

    residue_before = await app.verify_clean()
    assert residue_before == ["gmail drafts: 1 remaining", "gmail messages: 5 remaining"]

    fake.requests.clear()
    await app.reset(manifest)
    sequence = [(r.method, r.url.path.removeprefix("/gmail/v1/users/me")) for r in fake.requests]
    assert ("DELETE", "/drafts/r9") in sequence
    batch_index = sequence.index(("POST", "/messages/batchDelete"))
    assert sequence.index(("DELETE", "/drafts/r9")) < batch_index, "drafts go first"
    batch_body = _json(next(r for r in fake.requests if r.url.path.endswith("/messages/batchDelete")))
    assert set(batch_body["ids"]) == all_message_ids - {draft_message_id}, "the draft's message went with the draft"
    assert fake.messages == {} and fake.drafts == {}
    assert await app.verify_clean() == []
    await app.aclose()


@pytest.mark.asyncio
async def test_gmail_refuses_to_mutate_without_the_scratch_flag(
    monkeypatch: pytest.MonkeyPatch, mock_httpx: Any
) -> None:
    monkeypatch.delenv("BENCHPRESS_SCRATCH_OK", raising=False)
    fake = FakeGmail()
    mock_httpx(fake)
    app = GmailApp(_scratch_headers, address="scratch@benchpress-test.example", base_url="https://gmail.test")
    manifest = SeedManifest(scenario_id="unit", seeded_at=SEEDED_AT)
    with pytest.raises(ScratchGuardError):
        await app.seed(GMAIL_SEED, manifest)
    with pytest.raises(ScratchGuardError):
        await app.reset(manifest)
    assert fake.requests == []
    with pytest.raises(ValueError, match="email address"):
        GmailApp(_scratch_headers, address="not-an-address")
    await app.aclose()


# ============================================================================ Gmail OAuth


@pytest.mark.asyncio
async def test_gmail_token_provider_refreshes_and_caches_until_near_expiry() -> None:
    posts: list[httpx.Request] = []
    clock = {"now": 1000.0}

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(request)
        return httpx.Response(
            200, json={"access_token": f"tok{len(posts)}", "expires_in": 3600, "token_type": "Bearer"}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    env = {"GMAIL_CLIENT_ID": "cid", "GMAIL_CLIENT_SECRET": "sec", "GMAIL_REFRESH_TOKEN": "rt"}
    provider = GmailTokenProvider(env, client=client, now=lambda: clock["now"])
    assert not provider.uses_direct_token
    assert await provider.token() == "tok1"
    assert await provider.headers() == {"Authorization": "Bearer tok1"}
    assert len(posts) == 1
    assert str(posts[0].url) == "https://oauth2.googleapis.com/token"
    form = parse_qs(posts[0].content.decode())
    assert form == {
        "grant_type": ["refresh_token"],
        "client_id": ["cid"],
        "client_secret": ["sec"],
        "refresh_token": ["rt"],
    }

    clock["now"] = 1000.0 + 3600 - 61  # still fresh: one second before the 60 s refresh window
    assert await provider.token() == "tok1" and len(posts) == 1
    clock["now"] = 1000.0 + 3600 - 59  # inside the window: refresh
    assert await provider.token() == "tok2" and len(posts) == 2
    assert provider.refresh_count == 2
    await provider.aclose()
    await client.aclose()


@pytest.mark.asyncio
async def test_gmail_token_provider_direct_token_and_errors() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    direct = GmailTokenProvider({"GMAIL_ACCESS_TOKEN": "ya29.direct"}, client=client)
    assert direct.uses_direct_token
    assert await direct.token() == "ya29.direct"
    headers = gmail_headers_provider({"GMAIL_ACCESS_TOKEN": "ya29.direct"}, client=client)
    assert await headers() == {"Authorization": "Bearer ya29.direct"}

    failing = GmailTokenProvider(
        {"GMAIL_CLIENT_ID": "c", "GMAIL_CLIENT_SECRET": "s", "GMAIL_REFRESH_TOKEN": "r"}, client=client
    )
    with pytest.raises(GmailAuthError, match="HTTP 400"):
        await failing.token()
    with pytest.raises(GmailAuthError, match="GMAIL_REFRESH_TOKEN"):
        GmailTokenProvider({"GMAIL_CLIENT_ID": "c"})
    assert gmail_address({"GMAIL_ADDRESS": " scratch@benchpress-test.example "}) == "scratch@benchpress-test.example"
    with pytest.raises(GmailAuthError, match="GMAIL_ADDRESS"):
        gmail_address({})
    await client.aclose()
