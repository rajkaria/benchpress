"""Stripe seed / snapshot / reset driver: test mode only, or a devsim twin by base-URL swap.

Products (and their prices) are upserted by name and never reset; customers are created for
every seed and deleted on reset — the manifest's customers plus anything created since the seed
timestamp, so an agent's stray `POST /v1/customers` is cleaned up too. Deletes happen only here,
from the harness side. The agent's gateway never deletes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast
from urllib.parse import urlsplit

import httpx

from evals.realapps.base import RealAppClient, ScratchGuardError, SeedManifest, SeedResult, require_scratch_ok

STRIPE_BASE_URL = "https://api.stripe.com"
PAGE_LIMIT = 100
MAX_PAGES = 50
RESET_GRACE_SECONDS = 5
TEST_KEY_PREFIX = "sk_test_"


class StripeApiError(RuntimeError):
    def __init__(self, method: str, path: str, status_code: int, payload: Mapping[str, Any]) -> None:
        error = _mapping(payload.get("error"))
        detail = str(error.get("message") or error.get("type") or f"HTTP {status_code}")
        super().__init__(f"stripe {method} {path} failed ({status_code}): {detail}")
        self.status_code = status_code
        self.code = _optional_str(error.get("code"))
        self.payload = dict(payload)


class StripeApp:
    """`RealApp` for Stripe (test mode)."""

    app = "stripe"

    def __init__(
        self, secret_key: str, base_url: str = STRIPE_BASE_URL, *, client: RealAppClient | None = None
    ) -> None:
        if not secret_key.startswith(TEST_KEY_PREFIX):
            raise ScratchGuardError("Stripe key must be a test-mode secret key (sk_test_…); refusing to construct")
        self.base_url = base_url.rstrip("/")
        self._client = client or RealAppClient(self.base_url, {"Authorization": f"Bearer {secret_key}"})
        self._since: float | None = None
        self._loopback = _is_loopback(self.base_url)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> StripeApp:
        key = env.get("STRIPE_SECRET_KEY")
        if not key:
            raise ValueError("STRIPE_SECRET_KEY is not set (needed to seed/reset the Stripe test account)")
        return cls(key, base_url=env.get("DEVSIM_STRIPE_URL") or STRIPE_BASE_URL)

    def remember(self, manifest: SeedManifest) -> None:
        """Learn the seed timestamp from an earlier manifest (for verify without reset)."""
        self._since = manifest.seeded_at

    # -- RealApp -------------------------------------------------------------------------

    async def seed(self, seed_config: Mapping[str, Any], manifest: SeedManifest) -> SeedResult:
        self._guard()
        self._since = manifest.seeded_at
        config = _mapping(seed_config.get(self.app))
        notes: list[str] = []
        products_created = 0
        prices_created = 0
        by_name = {str(product.get("name")): product for product in await self._list_all("/v1/products")}
        for product in _mappings(config.get("products")):
            name = str(product["name"])
            existing = by_name.get(name)
            if existing is None:
                data: dict[str, str] = {"name": name}
                if product.get("description"):
                    data["description"] = str(product["description"])
                existing = await self._call("POST", "/v1/products", data=data)
                by_name[name] = existing
                products_created += 1
            product_id = str(existing["id"])
            prices = await self._list_all("/v1/prices", {"product": product_id, "active": "true"})
            if not prices:
                for price in _mappings(product.get("prices")):
                    await self._call(
                        "POST",
                        "/v1/prices",
                        data={
                            "product": product_id,
                            "currency": str(price["currency"]),
                            "unit_amount": str(price["unit_amount"]),
                        },
                    )
                    prices_created += 1
            manifest.alias(self.app, f"product:{name}", product_id)
        customers = 0
        for customer in _mappings(config.get("customers")):
            data = {"name": str(customer["name"]), "email": str(customer["email"])}
            if customer.get("description"):
                data["description"] = str(customer["description"])
            created = await self._call("POST", "/v1/customers", data=data)
            customer_id = str(created["id"])
            manifest.add(self.app, "customers", customer_id)
            manifest.alias(self.app, f"customer:{data['name']}", customer_id)
            customers += 1
        notes.append(f"products created: {products_created} (upserted by name, never reset)")
        notes.append(f"prices created: {prices_created}")
        return SeedResult(
            app=self.app,
            counts={"customers": customers, "products": len(_mappings(config.get("products")))},
            notes=tuple(notes),
        )

    async def snapshot(self) -> dict[str, Any]:
        customers = await self._list_all("/v1/customers")
        products = await self._list_all("/v1/products")
        prices = await self._list_all("/v1/prices")
        return {
            "customers": [
                {
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "email": item.get("email"),
                    "description": item.get("description"),
                    "metadata": item.get("metadata"),
                    "created": item.get("created"),
                }
                for item in customers
            ],
            "products": [
                {
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "active": item.get("active"),
                    "description": item.get("description"),
                    "metadata": item.get("metadata"),
                    "created": item.get("created"),
                }
                for item in products
            ],
            "prices": [
                {
                    "id": item.get("id"),
                    "product": item.get("product"),
                    "currency": item.get("currency"),
                    "unit_amount": item.get("unit_amount"),
                    "active": item.get("active"),
                    "created": item.get("created"),
                }
                for item in prices
            ],
        }

    async def reset(self, manifest: SeedManifest) -> None:
        self._guard()
        self._since = manifest.seeded_at
        deleted: set[str] = set()
        for customer_id in manifest.ids(self.app, "customers"):
            await self._delete_customer(customer_id)
            deleted.add(customer_id)
        for customer in await self._list_all("/v1/customers", {"created[gte]": str(self._created_floor())}):
            customer_id = str(customer["id"])
            if customer_id not in deleted:
                await self._delete_customer(customer_id)
                deleted.add(customer_id)

    async def verify_clean(self) -> list[str]:
        """Residue = customers created since the seed (all customers when no seed is known)."""
        params = {"created[gte]": str(self._created_floor())} if self._since is not None else {}
        return [
            f"stripe:customer {customer.get('id')} ({customer.get('email')}) still present"
            for customer in await self._list_all("/v1/customers", params)
        ]

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- Stripe API helpers ----------------------------------------------------------------

    async def _call(
        self,
        method: str,
        path: str,
        *,
        params: Sequence[tuple[str, str]] | None = None,
        data: Mapping[str, str] | None = None,
        tolerate_codes: Sequence[str] = (),
    ) -> dict[str, Any]:
        response = await self._client.request(method, path, params=params, data=data)
        payload = _json_object(response)
        if response.status_code >= 400:
            error = StripeApiError(method, path, response.status_code, payload)
            if error.code in tolerate_codes:
                return payload
            raise error
        return payload

    async def _list_all(self, path: str, params: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        starting_after: str | None = None
        for _ in range(MAX_PAGES):
            pairs = [(key, value) for key, value in (params or {}).items()]
            pairs.append(("limit", str(PAGE_LIMIT)))
            if starting_after is not None:
                pairs.append(("starting_after", starting_after))
            payload = await self._call("GET", path, params=pairs)
            page = _mappings(payload.get("data"))
            items.extend(page)
            if payload.get("has_more") is not True or not page:
                break
            starting_after = str(page[-1]["id"])
        return items

    async def _delete_customer(self, customer_id: str) -> None:
        await self._call("DELETE", f"/v1/customers/{customer_id}", tolerate_codes=("resource_missing",))

    def _created_floor(self) -> int:
        return int(self._since or 0) - RESET_GRACE_SECONDS

    def _guard(self) -> None:
        if not self._loopback:
            require_scratch_ok()


def from_env(env: Mapping[str, str]) -> StripeApp:
    return StripeApp.from_env(env)


# --------------------------------------------------------------------------------------


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
