"""HubSpot scratch-portal driver.

Seed: companies, contacts and deals from `seed_config["hubspot"]` through `POST /crm/v3/objects/{type}`, then
the seeded company<->contact associations through the v4 default-association route. HubSpot rejects unknown
property names, so a seeded property that is not a stock CRM property (for example `notes` on contacts) is
created once as a custom string property and recorded in the seed notes. `hubspot_owner_id` values referencing
seeded portal users cannot be created through the API and are dropped with a note.

Snapshot: every company, contact and deal (all seeded property names plus create/modify timestamps and
inline associations), plus notes and tasks, which are what an agent typically adds.
Reset: archive the manifest's ids, then archive every object of those types (and notes/tasks) created at or
after the seed timestamp, found through the search API. `verify_clean` expects an empty portal: the scratch
baseline is empty (run `wipe_samples()` once on a fresh portal to archive HubSpot's stock sample records).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, cast

import httpx

from evals.realapps.base import RealAppClient, SeedManifest, SeedResult, require_scratch_ok

BASE_URL = "https://api.hubapi.com"
OBJECT_TYPES: tuple[str, ...] = ("companies", "contacts", "deals")
ENGAGEMENT_TYPES: tuple[str, ...] = ("notes", "tasks")
RESET_SKEW_SECONDS = 120.0
PAGE_SIZE = 100
MAX_PAGES = 100
UNSEEDABLE_COLLECTIONS: tuple[str, ...] = ("lists", "tickets", "owners", "users")

# Stock CRM properties (never created); anything else seeded is created as a custom string property.
STOCK_PROPERTIES: dict[str, frozenset[str]] = {
    "companies": frozenset(
        {
            "name",
            "domain",
            "description",
            "hubspot_owner_id",
            "phone",
            "city",
            "state",
            "country",
            "industry",
            "lifecyclestage",
            "website",
            "numberofemployees",
            "annualrevenue",
            "type",
        }
    ),
    "contacts": frozenset(
        {
            "email",
            "firstname",
            "lastname",
            "lifecyclestage",
            "hubspot_owner_id",
            "phone",
            "company",
            "jobtitle",
            "website",
            "hs_lead_status",
            "city",
            "state",
            "country",
        }
    ),
    "deals": frozenset(
        {"dealname", "amount", "dealstage", "pipeline", "description", "hubspot_owner_id", "closedate", "dealtype"}
    ),
}
PROPERTY_GROUPS = {"companies": "companyinformation", "contacts": "contactinformation", "deals": "dealinformation"}
TEXTAREA_PROPERTIES = frozenset({"notes", "description", "body"})
CREATED_PROPERTY = {
    "companies": "createdate",
    "contacts": "createdate",
    "deals": "createdate",
    "notes": "hs_createdate",
    "tasks": "hs_createdate",
}
SNAPSHOT_PROPERTIES: dict[str, tuple[str, ...]] = {
    "companies": ("name", "domain", "description", "lifecyclestage", "hubspot_owner_id"),
    "contacts": ("email", "firstname", "lastname", "lifecyclestage", "hubspot_owner_id", "company"),
    "deals": ("dealname", "amount", "dealstage", "pipeline", "description", "hubspot_owner_id", "closedate"),
    "notes": ("hs_note_body", "hs_timestamp", "hubspot_owner_id"),
    "tasks": ("hs_task_subject", "hs_task_body", "hs_task_status", "hs_timestamp", "hubspot_owner_id"),
}
TIMESTAMP_PROPERTIES: dict[str, tuple[str, ...]] = {
    "companies": ("createdate", "hs_lastmodifieddate"),
    "contacts": ("createdate", "lastmodifieddate"),
    "deals": ("createdate", "hs_lastmodifieddate"),
    "notes": ("hs_createdate", "hs_lastmodifieddate"),
    "tasks": ("hs_createdate", "hs_lastmodifieddate"),
}
SNAPSHOT_ASSOCIATIONS: dict[str, tuple[str, ...]] = {
    "companies": ("contacts", "deals"),
    "contacts": ("companies",),
    "deals": ("companies", "contacts"),
    "notes": ("companies", "contacts", "deals"),
    "tasks": ("companies", "contacts", "deals"),
}
_EXISTING_ID = re.compile(r"Existing ID:\s*(\d+)")


class HubSpotSeedError(RuntimeError):
    def __init__(self, method: str, path: str, response: httpx.Response) -> None:
        super().__init__(f"hubspot {method} {path} -> HTTP {response.status_code}: {response.text[:300]}")
        self.status_code = response.status_code


def _records(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(cast(Mapping[str, Any], item)) for item in cast(list[object], value) if isinstance(item, Mapping)]


def _next_after(payload: Mapping[str, Any]) -> str | None:
    paging = payload.get("paging")
    if isinstance(paging, Mapping):
        next_page = cast(Mapping[str, Any], paging).get("next")
        if isinstance(next_page, Mapping):
            after = cast(Mapping[str, Any], next_page).get("after")
            if after:
                return str(after)
    return None


def _normalize(record: Mapping[str, Any]) -> dict[str, Any]:
    associations: dict[str, list[str]] = {}
    raw_associations = record.get("associations")
    if isinstance(raw_associations, Mapping):
        for to_type, block in cast(Mapping[str, Any], raw_associations).items():
            if isinstance(block, Mapping):
                results = _records(cast(Mapping[str, Any], block).get("results"))
                associations[str(to_type)] = sorted(str(item.get("id", "")) for item in results)
    properties = record.get("properties")
    return {
        "id": str(record.get("id", "")),
        "properties": dict(cast(Mapping[str, Any], properties)) if isinstance(properties, Mapping) else {},
        "createdAt": str(record.get("createdAt", "")),
        "updatedAt": str(record.get("updatedAt", "")),
        "archived": bool(record.get("archived", False)),
        "associations": associations,
    }


def _looks_like_sample(properties: Mapping[str, Any]) -> bool:
    text = " ".join(str(value or "") for value in properties.values()).lower()
    return "sample" in text or "hubspot.com" in text or text.startswith("hubspot")


class HubSpotApp:
    app = "hubspot"

    def __init__(self, token: str, base_url: str = BASE_URL) -> None:
        if not token:
            raise ValueError("a HubSpot private-app token is required")
        self._client = RealAppClient(base_url, {"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        self._seeded_properties: dict[str, set[str]] = {object_type: set() for object_type in OBJECT_TYPES}
        self._since: float | None = None
        self._manifest_ids: dict[str, list[str]] = {}

    def remember(self, manifest: SeedManifest) -> None:
        """Learn the seed timestamp and ids from an earlier manifest (for verify without reset)."""
        self._since = manifest.seeded_at
        self._manifest_ids = {object_type: list(manifest.ids("hubspot", object_type)) for object_type in OBJECT_TYPES}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json_body: object | None = None,
        expect: tuple[int, ...] = (200,),
    ) -> httpx.Response:
        response = await self._client.request(method, path, params=params, json_body=json_body)
        if response.status_code not in expect:
            raise HubSpotSeedError(method, path, response)
        return response

    # -- seed

    async def seed(self, seed_config: Mapping[str, Any], manifest: SeedManifest) -> SeedResult:
        require_scratch_ok()
        section = cast(Mapping[str, Any], seed_config.get("hubspot") or {})
        notes: list[str] = []
        counts: dict[str, int] = {}

        for object_type in OBJECT_TYPES:
            records = [
                dict(cast(Mapping[str, Any], cast(Mapping[str, Any], item).get("properties") or {}))
                for item in cast(list[Any], section.get(object_type) or [])
                if isinstance(item, Mapping)
            ]
            dropped_owner = False
            for properties in records:
                if properties.pop("hubspot_owner_id", None) is not None:
                    dropped_owner = True
            if dropped_owner:
                notes.append(f"hubspot {object_type}: hubspot_owner_id dropped (portal users are not seedable)")
            names = {name for properties in records for name in properties}
            self._seeded_properties[object_type] |= names
            for created_property in await self._ensure_properties(object_type, names):
                notes.append(f"hubspot {object_type}: created custom property {created_property!r}")
            for properties in records:
                object_id, note = await self._create(object_type, properties)
                if note:
                    notes.append(note)
                manifest.add("hubspot", object_type, object_id)
                for alias in self._aliases(object_type, properties):
                    manifest.alias("hubspot", alias, object_id)
            counts[object_type] = len(records)

        associations = [
            cast(Mapping[str, Any], item)
            for item in cast(list[Any], section.get("associations") or [])
            if isinstance(item, Mapping)
        ]
        for association in associations:
            company_domain = str(association.get("company_domain", ""))
            contact_email = str(association.get("contact_email", ""))
            company_id = manifest.aliases.get("hubspot", {}).get(f"company:{company_domain}")
            contact_id = manifest.aliases.get("hubspot", {}).get(f"contact:{contact_email}")
            if not company_id or not contact_id:
                raise HubSpotSeedError(
                    "seed",
                    "associations",
                    httpx.Response(400, text=f"unresolved association {company_domain!r} -> {contact_email!r}"),
                )
            await self._request(
                "PUT",
                f"/crm/v4/objects/companies/{company_id}/associations/default/contacts/{contact_id}",
                expect=(200, 201),
            )
            manifest.add("hubspot", "associations", f"{company_id}->{contact_id}")
        counts["associations"] = len(associations)

        for collection in UNSEEDABLE_COLLECTIONS:
            skipped = cast(list[Any], section.get(collection) or [])
            if skipped:
                notes.append(f"hubspot {collection}: {len(skipped)} entries not seeded (unsupported by this driver)")

        return SeedResult(app="hubspot", counts=counts, notes=tuple(notes))

    @staticmethod
    def _aliases(object_type: str, properties: Mapping[str, Any]) -> list[str]:
        aliases: list[str] = []
        if object_type == "companies":
            if properties.get("domain"):
                aliases.append(f"company:{properties['domain']}")
            if properties.get("name"):
                aliases.append(f"company:{properties['name']}")
        elif object_type == "contacts" and properties.get("email"):
            aliases.append(f"contact:{properties['email']}")
        elif object_type == "deals" and properties.get("dealname"):
            aliases.append(f"deal:{properties['dealname']}")
        return aliases

    async def _ensure_properties(self, object_type: str, names: set[str]) -> list[str]:
        custom = sorted(name for name in names if name not in STOCK_PROPERTIES[object_type])
        if not custom:
            return []
        payload = cast(Mapping[str, Any], (await self._request("GET", f"/crm/v3/properties/{object_type}")).json())
        existing = {str(item.get("name", "")) for item in _records(payload.get("results"))}
        created: list[str] = []
        for name in custom:
            if name in existing:
                continue
            await self._request(
                "POST",
                f"/crm/v3/properties/{object_type}",
                json_body={
                    "name": name,
                    "label": name.replace("_", " ").title(),
                    "type": "string",
                    "fieldType": "textarea" if name in TEXTAREA_PROPERTIES else "text",
                    "groupName": PROPERTY_GROUPS[object_type],
                },
                expect=(200, 201),
            )
            created.append(name)
        return created

    async def _create(self, object_type: str, properties: Mapping[str, Any]) -> tuple[str, str | None]:
        response = await self._request(
            "POST", f"/crm/v3/objects/{object_type}", json_body={"properties": dict(properties)}, expect=(200, 201, 409)
        )
        if response.status_code == 409:
            match = _EXISTING_ID.search(response.text)
            if match is None:
                raise HubSpotSeedError("POST", f"/crm/v3/objects/{object_type}", response)
            existing_id = match.group(1)
            await self._request(
                "PATCH", f"/crm/v3/objects/{object_type}/{existing_id}", json_body={"properties": dict(properties)}
            )
            return existing_id, f"hubspot {object_type} {existing_id} already existed; properties reset to the seed"
        return str(cast(Mapping[str, Any], response.json())["id"]), None

    # -- snapshot

    def _snapshot_properties(self, object_type: str) -> list[str]:
        names = set(SNAPSHOT_PROPERTIES[object_type]) | set(TIMESTAMP_PROPERTIES[object_type])
        names |= self._seeded_properties.get(object_type, set())
        return sorted(names)

    async def _list_page(
        self, object_type: str, *, properties: Sequence[str], associations: Sequence[str], after: str | None
    ) -> Mapping[str, Any]:
        params: dict[str, str] = {"limit": str(PAGE_SIZE), "properties": ",".join(properties), "archived": "false"}
        if associations:
            params["associations"] = ",".join(associations)
        if after:
            params["after"] = after
        return cast(
            Mapping[str, Any], (await self._request("GET", f"/crm/v3/objects/{object_type}", params=params)).json()
        )

    async def _list(self, object_type: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        after: str | None = None
        properties = self._snapshot_properties(object_type)
        associations = SNAPSHOT_ASSOCIATIONS[object_type]
        for _ in range(MAX_PAGES):
            payload = await self._list_page(object_type, properties=properties, associations=associations, after=after)
            results.extend(_normalize(record) for record in _records(payload.get("results")))
            after = _next_after(payload)
            if not after:
                break
        return results

    async def snapshot(self) -> dict[str, Any]:
        return {object_type: await self._list(object_type) for object_type in OBJECT_TYPES + ENGAGEMENT_TYPES}

    # -- reset

    async def _archive(self, object_type: str, object_id: str) -> None:
        await self._request("DELETE", f"/crm/v3/objects/{object_type}/{object_id}", expect=(204, 404))

    async def _created_since(self, object_type: str, since_ms: int) -> list[str]:
        created_property = CREATED_PROPERTY[object_type]
        ids: list[str] = []
        after: str | None = None
        for _ in range(MAX_PAGES):
            body: dict[str, Any] = {
                "filterGroups": [
                    {"filters": [{"propertyName": created_property, "operator": "GTE", "value": str(since_ms)}]}
                ],
                "properties": [created_property],
                "limit": PAGE_SIZE,
            }
            if after:
                body["after"] = after
            payload = cast(
                Mapping[str, Any],
                (await self._request("POST", f"/crm/v3/objects/{object_type}/search", json_body=body)).json(),
            )
            ids.extend(str(record.get("id", "")) for record in _records(payload.get("results")))
            after = _next_after(payload)
            if not after:
                break
        return [object_id for object_id in dict.fromkeys(ids) if object_id]

    async def reset(self, manifest: SeedManifest) -> None:
        require_scratch_ok()
        archived: set[tuple[str, str]] = set()
        for object_type in OBJECT_TYPES:
            for object_id in manifest.ids("hubspot", object_type):
                await self._archive(object_type, object_id)
                archived.add((object_type, object_id))
        since_ms = int((manifest.seeded_at - RESET_SKEW_SECONDS) * 1000)
        for object_type in OBJECT_TYPES + ENGAGEMENT_TYPES:
            for object_id in await self._created_since(object_type, since_ms):
                if (object_type, object_id) not in archived:
                    await self._archive(object_type, object_id)
                    archived.add((object_type, object_id))

    async def verify_clean(self) -> list[str]:
        """Residue is any seeded id still live, or any object created since the remembered seed timestamp.

        The scratch portal is shared with HubSpot's own inbox auto-import, so a non-empty portal is not
        residue by itself; only seed-era records count. Without a remembered manifest nothing is checked.
        """
        residue: list[str] = []
        for object_type, ids in self._manifest_ids.items():
            for object_id in ids:
                response = await self._request(
                    "GET", f"/crm/v3/objects/{object_type}/{object_id}", params={"archived": "false"}, expect=(200, 404)
                )
                if response.status_code == 200:
                    residue.append(f"hubspot {object_type}:{object_id} seeded record still present")
        if self._since is not None:
            since_ms = int((self._since - RESET_SKEW_SECONDS) * 1000)
            for object_type in OBJECT_TYPES + ENGAGEMENT_TYPES:
                ids = await self._created_since(object_type, since_ms)
                if ids:
                    residue.append(f"hubspot {object_type}: {len(ids)} created since seed (ids {', '.join(ids[:5])})")
        return residue

    async def wipe_samples(self) -> list[str]:
        """Archive HubSpot's stock sample records from a fresh portal. Only ever called explicitly."""
        require_scratch_ok()
        archived: list[str] = []
        for object_type in OBJECT_TYPES:
            for record in await self._list(object_type):
                if _looks_like_sample(cast(Mapping[str, Any], record["properties"])):
                    await self._archive(object_type, str(record["id"]))
                    archived.append(f"{object_type}:{record['id']}")
        return archived

    async def aclose(self) -> None:
        await self._client.aclose()


def from_env(env: Mapping[str, str]) -> HubSpotApp:
    token = env.get("HUBSPOT_PRIVATE_APP_TOKEN")
    if not token:
        raise ValueError("HUBSPOT_PRIVATE_APP_TOKEN is not set (needed to seed/reset the HubSpot scratch portal)")
    return HubSpotApp(token, base_url=env.get("DEVSIM_HUBSPOT_URL") or BASE_URL)
