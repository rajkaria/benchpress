"""Stripe twin: seeded shapes, Stripe's real envelopes, the grader's cardinality contract, unsafe stays observable.

The grader — not the Stripe docs — is the authority for `admin_state()`: `argabench_mkt_ecom_legacy`
counts new prices with `set(after) - set(before)` and removed customers with `_removed_mapping_count`,
both of which require every collection to be a **dict keyed by id**. `_record_collection` walks
`customers/products/prices/subscriptions` and compares whole records, so a read that mutated a record
(a counter, a `last_read_at`) would be scored as a protected-record change. Both properties are
asserted here directly against the vendored grader helpers where they are importable.
"""

from __future__ import annotations

import copy
import json
import sys
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from devsim.twins import available, load_spec
from devsim.twins.base import Store
from devsim.twins.stripe import (
    SPEC,
    STRIPE_API_VERSION,
    SearchQuery,
    StripeError,
    StripeStore,
    make_data_app,
    make_store,
)

ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = ROOT / "arga-twins-benchmark" / "benchmark" / "argabench_40" / "scenarios"
HARNESS_SRC = ROOT / "arga-twins-benchmark" / "src"

AUTH = {"Authorization": "Bearer sk_test_devsim_stripe"}
FORM = {"Content-Type": "application/x-www-form-urlencoded"}

# ECOM-02's published stripe slice: 4 customers, 3 products, 1 price each.
ECOM02_CUSTOMERS = 4
ECOM02_PRODUCTS = 3
ECOM02_PRICES = 3

BUSINESS_COLLECTIONS = ("customers", "products", "prices", "subscriptions")


def seed_slice(task: str = "ecom-02") -> dict[str, Any]:
    path = SCENARIOS / f"{task}.json"
    if not path.exists():  # pragma: no cover - only without the arga-twins-benchmark symlink
        pytest.skip(f"vendored scenario missing: {path}")
    scenario = cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
    return cast(dict[str, Any], scenario["seed_config"]["stripe"])


def build_store(seed_key: str = "stripe-test", task: str = "ecom-02") -> StripeStore:
    store = StripeStore(seed_key)
    store.seed({"stripe": seed_slice(task)})
    return store


def error_of(response: httpx.Response) -> dict[str, Any]:
    return cast(dict[str, Any], response.json()["error"])


def names(payload: Mapping[str, Any]) -> list[str]:
    return [str(record.get("name")) for record in cast(list[dict[str, Any]], payload["data"])]


def ids_of(payload: Mapping[str, Any]) -> list[str]:
    return [str(record["id"]) for record in cast(list[dict[str, Any]], payload["data"])]


@pytest.fixture
def store() -> StripeStore:
    return build_store()


@pytest.fixture
async def client(store: StripeStore) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=make_data_app(store))
    async with httpx.AsyncClient(transport=transport, base_url="http://stripe-twin", headers=AUTH) as session:
        yield session


@pytest.fixture
def customer_id(store: StripeStore) -> str:
    return next(cid for cid, record in store.customers.items() if record["name"] == "Northwind Studio")


# --------------------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------------------


def test_spec_is_discoverable_through_the_registry() -> None:
    assert "stripe" in available()
    spec = load_spec("stripe")
    assert spec is SPEC
    assert (spec.provider, spec.role) == ("stripe", "payments")


def test_make_store_returns_a_stripe_store() -> None:
    built = make_store("seed")
    assert isinstance(built, StripeStore)
    assert built.provider == "stripe"


def test_make_data_app_rejects_a_foreign_store() -> None:
    class Other(Store):
        provider = "other"

    with pytest.raises(TypeError):
        make_data_app(Other("seed"))


# --------------------------------------------------------------------------------------
# Seeding
# --------------------------------------------------------------------------------------


def test_seeding_ecom_02_yields_four_customers_three_products_three_prices(store: StripeStore) -> None:
    assert len(store.customers) == ECOM02_CUSTOMERS
    assert len(store.products) == ECOM02_PRODUCTS
    assert len(store.prices) == ECOM02_PRICES
    assert {record["email"] for record in store.customers.values()} == {
        entry["email"] for entry in seed_slice()["customers"]
    }
    assert {record["name"] for record in store.products.values()} == {
        entry["name"] for entry in seed_slice()["products"]
    }
    assert {int(record["unit_amount"]) for record in store.prices.values()} == {149000, 100, 9900}


def test_seeding_accepts_a_bare_provider_slice(store: StripeStore) -> None:
    direct = StripeStore("stripe-test")
    direct.seed(seed_slice())
    assert direct.admin_state()["customers"] == store.admin_state()["customers"]


def test_seeded_ids_are_deterministic_per_seed_key() -> None:
    first = build_store("seed-a")
    again = build_store("seed-a")
    other = build_store("seed-b")
    assert set(first.customers) == set(again.customers)
    assert set(first.customers).isdisjoint(other.customers)


def test_seeded_records_carry_stripe_id_formats_and_object_names(store: StripeStore) -> None:
    for cid, customer in store.customers.items():
        assert cid.startswith("cus_") and len(cid) == len("cus_") + 14
        assert customer["object"] == "customer"
        assert customer["livemode"] is False
    for pid in store.products:
        assert pid.startswith("prod_") and len(pid) == len("prod_") + 14
    for price_id, price in store.prices.items():
        assert price_id.startswith("price_1")
        assert price["object"] == "price"
        assert price["product"] in store.products
        assert price["unit_amount_decimal"] == str(price["unit_amount"])


def test_seeding_does_not_tick_the_write_clock(store: StripeStore) -> None:
    assert store.journal == []
    fresh = StripeStore("stripe-test")
    epoch_before = fresh.clock.epoch()
    fresh.seed({"stripe": seed_slice()})
    assert fresh.clock.epoch() == epoch_before
    # Seeded objects predate the scenario clock, so `created[gte]=now` filters them out cleanly.
    assert all(int(record["created"]) < epoch_before for record in fresh.customers.values())


# --------------------------------------------------------------------------------------
# Admin state: the grader's contract
# --------------------------------------------------------------------------------------


def test_admin_state_collections_are_dicts_keyed_by_id(store: StripeStore) -> None:
    state = store.admin_state()
    for name in BUSINESS_COLLECTIONS:
        collection = cast(dict[str, Any], state[name])
        assert isinstance(collection, dict)
        assert all(key == record["id"] for key, record in collection.items())


def test_admin_state_carries_the_real_twins_bookkeeping_keys(store: StripeStore) -> None:
    state = store.admin_state()
    assert state["idempotency"] == {"cached_responses": 0}
    assert state["generic_resources"] == {}
    assert state["counts"] == {"generic_resources": 0}


def test_admin_state_is_a_deep_copy(store: StripeStore) -> None:
    state = store.admin_state()
    cast(dict[str, Any], state["customers"]).clear()
    assert len(store.customers) == ECOM02_CUSTOMERS


@pytest.mark.anyio
async def test_grader_counts_new_prices_and_removed_customers(
    store: StripeStore, client: httpx.AsyncClient, customer_id: str
) -> None:
    """The two cardinality helpers the ECOM/MKT grader runs over stripe state must work on our shape."""
    if str(HARNESS_SRC) not in sys.path:  # pragma: no cover - import path setup
        sys.path.insert(0, str(HARNESS_SRC))
    legacy = pytest.importorskip("arga_twins_benchmark.reporting.argabench_mkt_ecom_legacy")

    before = {"providers": {"stripe": {"state": store.admin_state()}}}
    product_id = next(iter(store.products))
    created = await client.post("/v1/prices", data={"product": product_id, "currency": "usd", "unit_amount": "2500"})
    assert created.status_code == 200
    assert (await client.delete(f"/v1/customers/{customer_id}")).status_code == 200
    after = {"providers": {"stripe": {"state": store.admin_state()}}}

    removed = legacy._removed_mapping_count(  # pyright: ignore[reportPrivateUsage]
        legacy._provider_state(before, "stripe"),  # pyright: ignore[reportPrivateUsage]
        legacy._provider_state(after, "stripe"),  # pyright: ignore[reportPrivateUsage]
        ("customers",),
    )
    assert removed == 1
    prices_before = set(cast(dict[str, Any], before["providers"]["stripe"]["state"]["prices"]))
    prices_after = set(cast(dict[str, Any], after["providers"]["stripe"]["state"]["prices"]))
    assert prices_after - prices_before == {created.json()["id"]}


@pytest.mark.anyio
async def test_grader_sees_no_protected_change_after_reads_only(store: StripeStore, client: httpx.AsyncClient) -> None:
    if str(HARNESS_SRC) not in sys.path:  # pragma: no cover - import path setup
        sys.path.insert(0, str(HARNESS_SRC))
    legacy = pytest.importorskip("arga_twins_benchmark.reporting.argabench_mkt_ecom_legacy")

    before = {"providers": {"stripe": {"state": store.admin_state()}}}
    for path in ("/v1/customers", "/v1/products", "/v1/prices", "/v1/subscriptions", "/v1/invoices"):
        assert (await client.get(path)).status_code == 200
    assert (await client.get("/v1/customers/search", params={"query": "name~'northwind'"})).status_code == 200
    after = {"providers": {"stripe": {"state": store.admin_state()}}}

    assert (
        legacy._protected_change(before, after, "stripe", ["Northwind Studio"])  # pyright: ignore[reportPrivateUsage]
        is None
    )


# --------------------------------------------------------------------------------------
# Read purity
# --------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    "path",
    [
        "/v1/account",
        "/v1/customers",
        "/v1/products",
        "/v1/prices",
        "/v1/subscriptions",
        "/v1/invoices",
        "/v1/charges",
        "/v1/payment_intents",
        "/v1/refunds",
        "/v1/billing/meters",
        "/v1/events",
    ],
)
async def test_reads_never_change_state_or_the_clock(store: StripeStore, client: httpx.AsyncClient, path: str) -> None:
    snapshot = store.admin_state()
    epoch = store.clock.epoch()
    response = await client.get(path)
    assert response.status_code == 200
    assert store.admin_state() == snapshot
    assert store.clock.epoch() == epoch
    assert store.journal == []


@pytest.mark.anyio
async def test_retrieving_a_record_returns_a_copy(
    store: StripeStore, client: httpx.AsyncClient, customer_id: str
) -> None:
    payload = cast(dict[str, Any], (await client.get(f"/v1/customers/{customer_id}")).json())
    payload["name"] = "mutated"
    assert store.customers[customer_id]["name"] == "Northwind Studio"


@pytest.mark.anyio
async def test_request_ids_are_unique_but_are_not_state(store: StripeStore, client: httpx.AsyncClient) -> None:
    snapshot = store.admin_state()
    first = await client.get("/v1/customers")
    second = await client.get("/v1/customers")
    assert first.headers["Request-Id"] != second.headers["Request-Id"]
    assert first.headers["Stripe-Version"] == STRIPE_API_VERSION
    assert store.admin_state() == snapshot


# --------------------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("name:'Northwind Studio'", ["Northwind Studio"]),
        ("email:'billing@northwindstudio.example'", ["Northwind Studio"]),
        ("name~'northwind'", ["Northwind Studios Prospect", "Northwind Studio"]),
        ("name~'northwind' AND -email~'prospect'", ["Northwind Studios Prospect", "Northwind Studio"]),
        ("email~'northwind-studios'", ["Northwind Studios Prospect"]),
        (
            "email:'billing@northwindstudio.example' OR email:'hello@northwind-studios.example'",
            ["Northwind Studios Prospect", "Northwind Studio"],
        ),
        ("name~'northwind' AND email~'studios'", ["Northwind Studios Prospect"]),
        ("name:'nobody at all'", []),
    ],
)
async def test_customer_search_query_language(client: httpx.AsyncClient, query: str, expected: list[str]) -> None:
    response = await client.get("/v1/customers/search", params={"query": query})
    assert response.status_code == 200
    body = cast(dict[str, Any], response.json())
    assert body["object"] == "search_result"
    assert sorted(names(body)) == sorted(expected)


@pytest.mark.anyio
async def test_search_envelope_and_paging(client: httpx.AsyncClient) -> None:
    first = cast(
        dict[str, Any], (await client.get("/v1/customers/search", params={"query": "name~'i'", "limit": 1})).json()
    )
    assert first["object"] == "search_result"
    assert first["url"] == "/v1/customers/search"
    assert first["has_more"] is True
    assert first["next_page"] == "1"
    second = cast(
        dict[str, Any],
        (await client.get("/v1/customers/search", params={"query": "name~'i'", "limit": 1, "page": "1"})).json(),
    )
    assert ids_of(second) != ids_of(first)


@pytest.mark.anyio
async def test_search_supports_parentheses_metadata_and_total_count(
    client: httpx.AsyncClient, customer_id: str
) -> None:
    await client.post(f"/v1/customers/{customer_id}", content="metadata[tier]=gold", headers=FORM)
    by_metadata = await client.get("/v1/customers/search", params={"query": "metadata['tier']:'gold'"})
    assert ids_of(cast(dict[str, Any], by_metadata.json())) == [customer_id]

    grouped = await client.get(
        "/v1/customers/search", params={"query": "(name~'north' OR name~'sandbox') AND -name~'prospect'"}
    )
    assert sorted(names(cast(dict[str, Any], grouped.json()))) == [
        "Billing contact change reconciliation Sandbox",
        "Northwind Studio",
    ]

    plain = await client.get("/v1/customers/search", params={"query": "name~'north'"})
    assert "total_count" not in cast(dict[str, Any], plain.json())
    counted = await client.get("/v1/customers/search", params={"query": "name~'north'", "expand[]": "total_count"})
    assert counted.json()["total_count"] == 2


@pytest.mark.anyio
async def test_search_supports_numeric_comparisons(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/customers/search", params={"query": "created>0"})
    assert len(cast(list[Any], response.json()["data"])) == ECOM02_CUSTOMERS


@pytest.mark.anyio
async def test_expand_inlines_a_referenced_object(store: StripeStore, client: httpx.AsyncClient) -> None:
    price_id = next(iter(store.prices))
    plain = await client.get(f"/v1/prices/{price_id}")
    assert isinstance(plain.json()["product"], str)
    expanded = await client.get(f"/v1/prices/{price_id}", params={"expand[]": "product"})
    product = cast(dict[str, Any], expanded.json()["product"])
    assert product["object"] == "product"
    assert product["id"] == plain.json()["product"]


@pytest.mark.anyio
async def test_invoice_lifecycle_draft_open_void(
    store: StripeStore, client: httpx.AsyncClient, customer_id: str
) -> None:
    created = await client.post(
        "/v1/invoices", data={"customer": customer_id, "collection_method": "send_invoice", "days_until_due": "30"}
    )
    assert created.status_code == 200
    assert created.json()["status"] == "draft"
    invoice_id = str(created.json()["id"])
    assert (await client.post(f"/v1/invoices/{invoice_id}/finalize")).json()["status"] == "open"
    assert (await client.post(f"/v1/invoices/{invoice_id}/send")).json()["status"] == "open"
    assert (await client.post(f"/v1/invoices/{invoice_id}/void")).json()["status"] == "void"
    assert store.invoices[invoice_id]["status"] == "void"


@pytest.mark.anyio
async def test_search_supports_bare_terms_the_arga_twin_accepts(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/customers/search", params={"query": "Northwind"})
    assert response.status_code == 200
    assert sorted(names(cast(dict[str, Any], response.json()))) == ["Northwind Studio", "Northwind Studios Prospect"]


@pytest.mark.anyio
async def test_search_without_a_query_is_a_parameter_missing_400(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/customers/search")
    assert response.status_code == 400
    assert error_of(response)["code"] == "parameter_missing"
    assert error_of(response)["param"] == "query"


@pytest.mark.anyio
async def test_unparseable_search_query_is_a_400(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/customers/search", params={"query": "name:'x' AND"})
    assert response.status_code == 400
    assert error_of(response)["param"] == "query"


def test_search_query_compiles_and_matches_without_http() -> None:
    query = SearchQuery("name~'north' AND -name~'prospect'", "customers")
    assert query.matches({"name": "Northwind Studio"}) is True
    assert query.matches({"name": "Northwind Studios Prospect"}) is False
    with pytest.raises(StripeError):
        SearchQuery("   ", "customers")


@pytest.mark.anyio
async def test_product_and_price_search(client: httpx.AsyncClient, store: StripeStore) -> None:
    products = await client.get("/v1/products/search", params={"query": "name:'Studio Annual'"})
    assert names(cast(dict[str, Any], products.json())) == ["Studio Annual"]
    product_id = next(pid for pid, record in store.products.items() if record["name"] == "Studio Annual")
    prices = await client.get("/v1/prices/search", params={"query": f"product:'{product_id}'"})
    assert len(cast(list[Any], prices.json()["data"])) == 1


# --------------------------------------------------------------------------------------
# Form bodies and updates
# --------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_update_via_form_body_echoes_the_full_customer(
    store: StripeStore, client: httpx.AsyncClient, customer_id: str
) -> None:
    response = await client.post(
        f"/v1/customers/{customer_id}",
        content="email=ap%40northwindstudio.example&metadata[owner]=ap",
        headers=FORM,
    )
    assert response.status_code == 200
    body = cast(dict[str, Any], response.json())
    assert body["id"] == customer_id
    assert body["object"] == "customer"
    assert body["email"] == "ap@northwindstudio.example"
    assert body["metadata"] == {"owner": "ap"}
    # The echo is the whole record, not a patch: untouched fields come back too.
    assert body["name"] == "Northwind Studio"
    assert set(body) == set(store.customers[customer_id])
    assert body == store.customers[customer_id]


@pytest.mark.anyio
async def test_form_bracket_notation_decodes_nested_hashes(client: httpx.AsyncClient, customer_id: str) -> None:
    response = await client.post(
        f"/v1/customers/{customer_id}",
        content="address[line1]=1+Quay+St&address[city]=Auckland&address[country]=NZ",
        headers=FORM,
    )
    assert response.status_code == 200
    address = cast(dict[str, Any], response.json()["address"])
    assert address["line1"] == "1 Quay St"
    assert address["city"] == "Auckland"
    assert address["country"] == "NZ"


@pytest.mark.anyio
async def test_json_bodies_are_accepted_too(client: httpx.AsyncClient, customer_id: str) -> None:
    response = await client.post(f"/v1/customers/{customer_id}", json={"description": "primary billing contact"})
    assert response.status_code == 200
    assert response.json()["description"] == "primary billing contact"


@pytest.mark.anyio
async def test_metadata_merges_and_empty_string_unsets(client: httpx.AsyncClient, customer_id: str) -> None:
    await client.post(f"/v1/customers/{customer_id}", content="metadata[a]=1&metadata[b]=2", headers=FORM)
    merged = await client.post(f"/v1/customers/{customer_id}", content="metadata[b]=&metadata[c]=3", headers=FORM)
    assert merged.json()["metadata"] == {"a": "1", "c": "3"}
    cleared = await client.post(f"/v1/customers/{customer_id}", content="description=", headers=FORM)
    assert cleared.json()["description"] is None


@pytest.mark.anyio
async def test_a_write_journals_a_mutation_and_ticks_the_clock(
    store: StripeStore, client: httpx.AsyncClient, customer_id: str
) -> None:
    epoch = store.clock.epoch()
    await client.post(f"/v1/customers/{customer_id}", content="name=Northwind+Studio+Ltd", headers=FORM)
    assert store.clock.epoch() == epoch + 1
    assert len(store.journal) == 1
    mutation = store.journal[0]
    assert (mutation.method, mutation.collection, mutation.record_id) == ("POST", "customers", customer_id)
    assert cast(dict[str, Any], mutation.before)["name"] == "Northwind Studio"
    assert cast(dict[str, Any], mutation.after)["name"] == "Northwind Studio Ltd"


@pytest.mark.anyio
async def test_a_write_emits_an_event_with_previous_attributes(
    store: StripeStore, client: httpx.AsyncClient, customer_id: str
) -> None:
    await client.post(f"/v1/customers/{customer_id}", content="name=Renamed", headers=FORM)
    event = store.events[-1]
    assert event["type"] == "customer.updated"
    assert cast(dict[str, Any], event["data"])["previous_attributes"] == {"name": "Northwind Studio"}
    listed = await client.get("/v1/events", params={"type": "customer.updated"})
    assert ids_of(cast(dict[str, Any], listed.json())) == [event["id"]]


@pytest.mark.anyio
async def test_invalid_email_is_rejected_without_touching_state(
    store: StripeStore, client: httpx.AsyncClient, customer_id: str
) -> None:
    snapshot = store.admin_state()
    response = await client.post(f"/v1/customers/{customer_id}", content="email=not-an-email", headers=FORM)
    assert response.status_code == 400
    assert error_of(response)["code"] == "email_invalid"
    assert store.admin_state() == snapshot


# --------------------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_idempotency_key_replays_the_first_response(store: StripeStore, client: httpx.AsyncClient) -> None:
    body = {"amount": "4900", "currency": "usd", "source": "tok_visa"}
    first = await client.post("/v1/charges", data=body, headers={"Idempotency-Key": "charge-once"})
    assert first.status_code == 200
    replay = await client.post("/v1/charges", data=body, headers={"Idempotency-Key": "charge-once"})
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert replay.headers["Original-Request"] == first.headers["Request-Id"]
    assert len(store.charges) == 1
    assert store.admin_state()["idempotency"] == {"cached_responses": 1}


@pytest.mark.anyio
async def test_reusing_a_key_with_different_parameters_is_an_idempotency_error(client: httpx.AsyncClient) -> None:
    first = await client.post(
        "/v1/charges", data={"amount": "100", "currency": "usd", "source": "tok_visa"}, headers={"Idempotency-Key": "k"}
    )
    assert first.status_code == 200
    conflict = await client.post(
        "/v1/charges", data={"amount": "200", "currency": "usd", "source": "tok_visa"}, headers={"Idempotency-Key": "k"}
    )
    assert conflict.status_code == 400
    assert error_of(conflict)["type"] == "idempotency_error"


@pytest.mark.anyio
async def test_idempotency_key_is_ignored_on_reads(store: StripeStore, client: httpx.AsyncClient) -> None:
    await client.get("/v1/customers", headers={"Idempotency-Key": "read"})
    assert store.admin_state()["idempotency"] == {"cached_responses": 0}


@pytest.mark.anyio
async def test_errors_are_cached_under_their_key_too(client: httpx.AsyncClient) -> None:
    first = await client.post("/v1/charges", data={"currency": "usd"}, headers={"Idempotency-Key": "bad"})
    assert first.status_code == 400
    replay = await client.post("/v1/charges", data={"currency": "usd"}, headers={"Idempotency-Key": "bad"})
    assert replay.status_code == 400
    assert replay.json() == first.json()


# --------------------------------------------------------------------------------------
# Error envelopes
# --------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_missing_object_in_the_url_is_a_404_resource_missing(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/customers/cus_doesnotexist")
    assert response.status_code == 404
    error = error_of(response)
    assert error["type"] == "invalid_request_error"
    assert error["code"] == "resource_missing"
    assert error["param"] == "id"
    assert error["message"] == "No such customer: 'cus_doesnotexist'"
    assert error["doc_url"] == "https://stripe.com/docs/error-codes/resource-missing"
    assert error["request_log_url"].endswith(response.headers["Request-Id"])


@pytest.mark.anyio
async def test_missing_object_named_by_a_parameter_is_a_400(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/prices", data={"product": "prod_missing", "currency": "usd", "unit_amount": "1"})
    assert response.status_code == 400
    assert error_of(response)["code"] == "resource_missing"
    assert error_of(response)["param"] == "product"


@pytest.mark.anyio
async def test_unknown_parameter_is_a_400(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/customers", data={"nope": "1"})
    assert response.status_code == 400
    assert error_of(response)["code"] == "parameter_unknown"
    assert error_of(response)["param"] == "nope"


@pytest.mark.anyio
async def test_missing_required_parameter_is_a_400(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/charges", data={"currency": "usd"})
    assert response.status_code == 400
    assert error_of(response)["code"] == "parameter_missing"
    assert error_of(response)["param"] == "amount"


@pytest.mark.anyio
async def test_unrecognized_url_is_a_404_without_a_code(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/not/a/real/route")
    assert response.status_code == 404
    error = error_of(response)
    assert "Unrecognized request URL (GET: /v1/not/a/real/route)" in error["message"]
    assert "code" not in error


@pytest.mark.anyio
async def test_a_missing_api_key_is_a_401(store: StripeStore) -> None:
    transport = httpx.ASGITransport(app=make_data_app(store))
    async with httpx.AsyncClient(transport=transport, base_url="http://stripe-twin") as anonymous:
        response = await anonymous.get("/v1/customers")
    assert response.status_code == 401
    assert "did not provide an API key" in error_of(response)["message"]


@pytest.mark.anyio
async def test_an_unknown_list_route_returns_an_empty_list_and_records_a_generic_resource(
    store: StripeStore, client: httpx.AsyncClient
) -> None:
    response = await client.get("/v1/coupons")
    assert response.status_code == 200
    assert response.json() == {"object": "list", "data": [], "has_more": False, "url": "/v1/coupons"}
    state = store.admin_state()
    assert state["generic_resources"] == {"/v1/coupons": {}}
    assert state["counts"] == {"generic_resources": 1}
    for name in BUSINESS_COLLECTIONS:
        assert cast(dict[str, Any], state[name]) == cast(dict[str, Any], store.admin_state()[name])


# --------------------------------------------------------------------------------------
# List envelope and pagination
# --------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_list_envelope_shape(client: httpx.AsyncClient) -> None:
    body = cast(dict[str, Any], (await client.get("/v1/customers")).json())
    assert set(body) == {"object", "data", "has_more", "url"}
    assert body["object"] == "list"
    assert body["has_more"] is False
    assert body["url"] == "/v1/customers"
    assert len(cast(list[Any], body["data"])) == ECOM02_CUSTOMERS


@pytest.mark.anyio
async def test_lists_are_newest_first_and_pageable(client: httpx.AsyncClient) -> None:
    everything = ids_of(cast(dict[str, Any], (await client.get("/v1/customers")).json()))
    page_one = cast(dict[str, Any], (await client.get("/v1/customers", params={"limit": 2})).json())
    assert ids_of(page_one) == everything[:2]
    assert page_one["has_more"] is True
    page_two = cast(
        dict[str, Any],
        (await client.get("/v1/customers", params={"limit": 2, "starting_after": everything[1]})).json(),
    )
    assert ids_of(page_two) == everything[2:]
    assert page_two["has_more"] is False
    backwards = cast(
        dict[str, Any], (await client.get("/v1/customers", params={"ending_before": everything[2]})).json()
    )
    assert ids_of(backwards) == everything[:2]


@pytest.mark.anyio
async def test_pagination_rejects_a_bad_limit_and_an_unknown_cursor(client: httpx.AsyncClient) -> None:
    too_big = await client.get("/v1/customers", params={"limit": 500})
    assert too_big.status_code == 400
    assert error_of(too_big)["param"] == "limit"
    bad_cursor = await client.get("/v1/customers", params={"starting_after": "cus_nothere"})
    assert bad_cursor.status_code == 400
    assert error_of(bad_cursor)["param"] == "starting_after"


@pytest.mark.anyio
async def test_lists_filter_by_relationship_and_created_bounds(store: StripeStore, client: httpx.AsyncClient) -> None:
    product_id = next(pid for pid, record in store.products.items() if record["name"] == "Studio Annual")
    filtered = await client.get("/v1/prices", params={"product": product_id})
    assert len(cast(list[Any], filtered.json()["data"])) == 1
    none_yet = await client.get("/v1/customers", params={"created[gte]": store.clock.epoch()})
    assert cast(list[Any], none_yet.json()["data"]) == []


# --------------------------------------------------------------------------------------
# Unsafe routes work and stay observable
# --------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_deleting_a_customer_removes_it_from_admin_state(
    store: StripeStore, client: httpx.AsyncClient, customer_id: str
) -> None:
    response = await client.delete(f"/v1/customers/{customer_id}")
    assert response.status_code == 200
    assert response.json() == {"id": customer_id, "object": "customer", "deleted": True}
    assert customer_id not in cast(dict[str, Any], store.admin_state()["customers"])
    assert len(store.customers) == ECOM02_CUSTOMERS - 1
    assert store.journal[-1].method == "DELETE"
    assert store.journal[-1].after is None
    assert store.events[-1]["type"] == "customer.deleted"


@pytest.mark.anyio
async def test_charging_a_card_succeeds_and_is_visible(
    store: StripeStore, client: httpx.AsyncClient, customer_id: str
) -> None:
    response = await client.post(
        "/v1/charges", data={"amount": "14900", "currency": "usd", "customer": customer_id, "source": "tok_visa"}
    )
    assert response.status_code == 200
    charge = cast(dict[str, Any], response.json())
    assert charge["object"] == "charge"
    assert charge["status"] == "succeeded"
    assert charge["paid"] is True
    assert charge["amount"] == 14900
    assert charge["customer"] == customer_id
    assert store.charges[charge["id"]]["id"] == charge["id"]
    assert store.journal[-1].collection == "charges"
    fetched = await client.get(f"/v1/charges/{charge['id']}")
    assert fetched.json()["id"] == charge["id"]


@pytest.mark.anyio
async def test_payment_intent_create_confirm_and_capture(store: StripeStore, client: httpx.AsyncClient) -> None:
    created = await client.post(
        "/v1/payment_intents", data={"amount": "2500", "currency": "usd", "capture_method": "manual"}
    )
    assert created.status_code == 200
    intent_id = str(created.json()["id"])
    assert intent_id.startswith("pi_3")
    confirmed = await client.post(f"/v1/payment_intents/{intent_id}/confirm", data={"payment_method": "pm_card_visa"})
    assert confirmed.json()["status"] == "requires_capture"
    captured = await client.post(f"/v1/payment_intents/{intent_id}/capture")
    assert captured.json()["status"] == "succeeded"
    assert store.payment_intents[intent_id]["status"] == "succeeded"
    assert {str(event["type"]) for event in store.events} >= {"payment_intent.created", "payment_intent.succeeded"}


@pytest.mark.anyio
async def test_refunding_a_charge_works(store: StripeStore, client: httpx.AsyncClient) -> None:
    charge = cast(
        dict[str, Any],
        (await client.post("/v1/charges", data={"amount": "500", "currency": "usd", "source": "tok_visa"})).json(),
    )
    refund = await client.post("/v1/refunds", data={"charge": charge["id"]})
    assert refund.status_code == 200
    assert refund.json()["status"] == "succeeded"
    assert store.charges[charge["id"]]["refunded"] is True


@pytest.mark.anyio
async def test_creating_a_price_is_a_new_record_not_an_edit(store: StripeStore, client: httpx.AsyncClient) -> None:
    before = copy.deepcopy(cast(dict[str, Any], store.admin_state()["prices"]))
    product_id = next(iter(store.products))
    response = await client.post(
        "/v1/prices",
        content=f"product={product_id}&currency=usd&unit_amount=4900&recurring[interval]=month",
        headers=FORM,
    )
    assert response.status_code == 200
    price = cast(dict[str, Any], response.json())
    assert price["type"] == "recurring"
    assert cast(dict[str, Any], price["recurring"])["interval"] == "month"
    after = cast(dict[str, Any], store.admin_state()["prices"])
    assert set(after) - set(before) == {price["id"]}
    assert all(after[key] == value for key, value in before.items())


@pytest.mark.anyio
async def test_deleting_a_product_with_prices_is_refused_like_stripe(
    store: StripeStore, client: httpx.AsyncClient
) -> None:
    product_id = next(iter(store.products))
    response = await client.delete(f"/v1/products/{product_id}")
    assert response.status_code == 400
    assert "cannot be deleted because it has one or more user-created prices" in error_of(response)["message"]
    assert product_id in store.products


@pytest.mark.anyio
async def test_subscribing_to_a_one_time_price_is_refused(
    store: StripeStore, client: httpx.AsyncClient, customer_id: str
) -> None:
    price_id = next(pid for pid, price in store.prices.items() if price["type"] == "one_time")
    response = await client.post(
        "/v1/subscriptions", content=f"customer={customer_id}&items[0][price]={price_id}", headers=FORM
    )
    assert response.status_code == 400
    assert error_of(response)["param"] == "items[0][price]"
    assert store.subscriptions == {}


@pytest.mark.anyio
async def test_charging_without_a_source_or_customer_is_refused(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/charges", data={"amount": "500", "currency": "usd"})
    assert response.status_code == 400
    assert error_of(response)["message"] == "Must provide source or customer."


@pytest.mark.anyio
async def test_subscription_create_and_cancel(store: StripeStore, client: httpx.AsyncClient, customer_id: str) -> None:
    product_id = next(iter(store.products))
    recurring = await client.post(
        "/v1/prices",
        content=f"product={product_id}&currency=usd&unit_amount=14900&recurring[interval]=year",
        headers=FORM,
    )
    price_id = str(recurring.json()["id"])
    created = await client.post(
        "/v1/subscriptions", content=f"customer={customer_id}&items[0][price]={price_id}", headers=FORM
    )
    assert created.status_code == 200
    subscription_id = str(created.json()["id"])
    canceled = await client.delete(f"/v1/subscriptions/{subscription_id}")
    assert canceled.status_code == 200
    assert canceled.json()["status"] == "canceled"
    assert store.subscriptions[subscription_id]["status"] == "canceled"
