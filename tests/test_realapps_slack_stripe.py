"""Slack and Stripe seed/snapshot/reset drivers, against in-memory fakes of the two APIs.

No network: `MockRealAppClient` routes `RealAppClient` through `httpx.MockTransport`, so the
tests also exercise the shared client's retry path. Every entity is invented.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import httpx
import pytest

from evals import seed as seed_cli
from evals.realapps.base import RealAppClient, ScratchGuardError, SeedManifest, SeedResult
from evals.realapps.slack import SEEDED_ICON, SlackApiError, SlackApp
from evals.realapps.stripe import StripeApp
from evals.seed import build_apps, expected_counts, format_counts, load_task_seed_config

Handler = Any  # Callable[[httpx.Request], httpx.Response]; kept loose for the fake servers below.

SLACK_SEED: dict[str, Any] = {
    "slack": {
        "channels": [
            {
                "name": "ops-desk",
                "messages": [
                    {"text": "Rivermill Studio asked to move renewal notices to accounts payable.", "user": "marlon"},
                    {"text": "Nightly archival completed.", "user": "ops-bot-human"},
                ],
            },
            {"name": "announcements", "messages": [{"text": "Guest network maintenance at 18:30.", "user": "marlon"}]},
        ],
        "users": [
            {"name": "marlon", "real_name": "Marlon Vega"},
            {"name": "ops-bot-human", "real_name": "Ops Coordinator"},
        ],
    }
}

STRIPE_SEED: dict[str, Any] = {
    "stripe": {
        "customers": [
            {"name": "Rivermill Studio", "email": "billing@rivermill.example"},
            {"name": "Rivermill Studios Prospect", "email": "hello@rivermill-studios.example"},
        ],
        "products": [
            {"name": "Alpha Plan", "prices": [{"currency": "usd", "unit_amount": 149000}]},
            {"name": "Beta Plan", "prices": [{"currency": "usd", "unit_amount": 9900}]},
        ],
        "meters": [],
    }
}


class MockRealAppClient(RealAppClient):
    """The shared client, with its transport swapped for an in-memory fake."""

    def __init__(self, base_url: str, headers: Mapping[str, str], handler: Handler) -> None:
        super().__init__(base_url, headers)
        self._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=self.base_url, headers=self._headers
        )


# -- fake Slack ---------------------------------------------------------------------------------------


class FakeSlack:
    USER_ID = "U0BOT"
    BOT_ID = "B0BOT"

    def __init__(self) -> None:
        self.channels: dict[str, dict[str, Any]] = {}
        self.messages: dict[str, list[dict[str, Any]]] = {}
        self.requests: list[httpx.Request] = []
        self.rate_limit_next_post = False
        self.fail_with: str | None = None
        self._ts = time.time()
        self._seq = 0

    def add_channel(self, name: str, *, is_member: bool = False, archived: bool = False) -> str:
        channel_id = f"C{len(self.channels) + 1:04d}"
        self.channels[channel_id] = {"id": channel_id, "name": name, "is_member": is_member, "is_archived": archived}
        self.messages[channel_id] = []
        return channel_id

    def next_ts(self) -> str:
        self._seq += 1
        return f"{int(self._ts)}.{self._seq:06d}"

    def add_message(self, channel_id: str, text: str, *, ours: bool, thread_ts: str | None = None) -> str:
        ts = self.next_ts()
        message: dict[str, Any] = {"type": "message", "text": text, "ts": ts}
        if ours:
            message.update({"subtype": "bot_message", "bot_id": self.BOT_ID, "username": "Agent"})
        else:
            message["user"] = "U0HUMAN"
        if thread_ts is not None:
            message["thread_ts"] = thread_ts
            parent = next(m for m in self.messages[channel_id] if m["ts"] == thread_ts)
            parent["reply_count"] = parent.get("reply_count", 0) + 1
        self.messages[channel_id].append(message)
        return ts

    def bot_messages(self, channel_id: str) -> list[dict[str, Any]]:
        return [m for m in self.messages[channel_id] if m.get("bot_id") == self.BOT_ID]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        method = request.url.path.removeprefix("/api/")
        if self.fail_with is not None:
            return _slack_error(self.fail_with)
        params = dict(request.url.params)
        body: dict[str, Any] = json.loads(request.content) if request.content else {}
        if method == "auth.test":
            return _slack_ok({"user_id": self.USER_ID, "bot_id": self.BOT_ID, "team": "T0SCRATCH"})
        if method == "conversations.list":
            return _slack_ok({"channels": list(self.channels.values()), "response_metadata": {"next_cursor": ""}})
        if method == "conversations.create":
            if any(c["name"] == body["name"] for c in self.channels.values()):
                return _slack_error("name_taken")
            channel_id = self.add_channel(str(body["name"]), is_member=True)
            return _slack_ok({"channel": self.channels[channel_id]})
        if method == "conversations.join":
            self.channels[body["channel"]]["is_member"] = True
            return _slack_ok({"channel": self.channels[body["channel"]]})
        if method == "conversations.unarchive":
            self.channels[body["channel"]]["is_archived"] = False
            return _slack_ok({})
        if method == "chat.postMessage":
            if self.rate_limit_next_post:
                self.rate_limit_next_post = False
                return httpx.Response(429, headers={"Retry-After": "0"}, json={"ok": False, "error": "ratelimited"})
            ts = self.next_ts()
            message = {
                "type": "message",
                "subtype": "bot_message",
                "bot_id": self.BOT_ID,
                "username": body.get("username"),
                "text": body["text"],
                "ts": ts,
            }
            if body.get("thread_ts"):
                message["thread_ts"] = body["thread_ts"]
            self.messages[body["channel"]].append(message)
            return _slack_ok({"ts": ts, "channel": body["channel"], "message": message})
        if method == "chat.delete":
            queue = self.messages[body["channel"]]
            before = len(queue)
            queue[:] = [m for m in queue if m["ts"] != body["ts"]]
            return _slack_ok({"ts": body["ts"]}) if len(queue) < before else _slack_error("message_not_found")
        if method == "conversations.history":
            oldest = float(params.get("oldest", "0"))
            history = [m for m in self.messages[params["channel"]] if "thread_ts" not in m and float(m["ts"]) >= oldest]
            history.sort(key=lambda m: float(m["ts"]), reverse=True)
            return _slack_ok({"messages": history, "has_more": False, "response_metadata": {"next_cursor": ""}})
        if method == "conversations.replies":
            thread = [
                m
                for m in self.messages[params["channel"]]
                if m["ts"] == params["ts"] or m.get("thread_ts") == params["ts"]
            ]
            return _slack_ok({"messages": thread, "has_more": False, "response_metadata": {"next_cursor": ""}})
        if method == "users.list":
            members = [
                {"id": self.USER_ID, "name": "benchpress", "real_name": "Benchpress", "is_bot": True, "deleted": False}
            ]
            return _slack_ok({"members": members, "response_metadata": {"next_cursor": ""}})
        return _slack_error("unknown_method")


def _slack_ok(payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, **payload})


def _slack_error(error: str) -> httpx.Response:
    return httpx.Response(200, json={"ok": False, "error": error})


def slack_app(fake: FakeSlack, base_url: str = "http://127.0.0.1:1") -> SlackApp:
    client = MockRealAppClient(base_url, {"Authorization": "Bearer xoxb-unit"}, fake)
    return SlackApp("xoxb-unit", base_url=base_url, client=client)


def _slack_calls(fake: FakeSlack, method: str) -> list[httpx.Request]:
    return [r for r in fake.requests if r.url.path == f"/api/{method}"]


# -- Slack tests --------------------------------------------------------------------------------------


async def test_slack_seed_posts_under_seeded_display_names_and_records_the_manifest() -> None:
    fake = FakeSlack()
    existing_id = fake.add_channel("ops-desk")
    app = slack_app(fake)
    manifest = SeedManifest(scenario_id="unit")

    result = await app.seed(SLACK_SEED, manifest)

    assert result == SeedResult(app="slack", counts={"channels": 2, "messages": 3}, notes=result.notes)
    assert any("created #announcements" in note for note in result.notes)
    created_id = next(cid for cid, c in fake.channels.items() if c["name"] == "announcements")
    assert manifest.aliases["slack"] == {"ops-desk": existing_id, "announcements": created_id}
    assert manifest.ids("slack", "channels") == [created_id]
    assert manifest.counts()["slack"]["messages"] == 3
    assert all(":" in ref for ref in manifest.ids("slack", "messages"))
    assert manifest.ids("slack", "messages")[0].startswith(f"{existing_id}:")
    assert fake.channels[existing_id]["is_member"] is True

    posts = _slack_calls(fake, "chat.postMessage")
    assert len(posts) == 3 and len(_slack_calls(fake, "conversations.create")) == 1
    assert len(_slack_calls(fake, "conversations.join")) == 2
    first = json.loads(posts[0].content)
    assert first == {
        "channel": existing_id,
        "text": "Rivermill Studio asked to move renewal notices to accounts payable.",
        "username": "Marlon Vega",
        "icon_emoji": SEEDED_ICON,
    }
    assert posts[0].headers["content-type"].startswith("application/json")
    assert posts[0].headers["authorization"] == "Bearer xoxb-unit"
    assert json.loads(posts[1].content)["username"] == "Ops Coordinator"
    await app.aclose()


async def test_slack_seed_survives_a_rate_limit_via_the_shared_client_retry() -> None:
    fake = FakeSlack()
    fake.rate_limit_next_post = True
    app = slack_app(fake)
    result = await app.seed(SLACK_SEED, SeedManifest(scenario_id="unit"))
    assert result.counts["messages"] == 3
    assert len(_slack_calls(fake, "chat.postMessage")) == 4
    await app.aclose()


async def test_slack_reset_deletes_manifest_messages_and_newer_bot_messages_only() -> None:
    fake = FakeSlack()
    app = slack_app(fake)
    manifest = SeedManifest(scenario_id="unit", seeded_at=time.time() - 1.0)
    await app.seed(SLACK_SEED, manifest)
    ops = manifest.aliases["slack"]["ops-desk"]
    human_ts = fake.add_message(ops, "Human reply, must stay.", ours=False)
    agent_ts = fake.add_message(ops, "Agent update posted during the trial.", ours=True)
    seeded_parent = manifest.ids("slack", "messages")[0].split(":")[1]
    reply_ts = fake.add_message(ops, "Agent thread reply.", ours=True, thread_ts=seeded_parent)

    await app.reset(manifest)

    remaining = {m["ts"] for m in fake.messages[ops]}
    assert remaining == {human_ts}
    assert fake.bot_messages(ops) == []
    deleted = {json.loads(r.content)["ts"] for r in _slack_calls(fake, "chat.delete")}
    assert {agent_ts, reply_ts, seeded_parent} <= deleted and human_ts not in deleted
    assert len(deleted) == 5
    assert await app.verify_clean() == []
    await app.aclose()


async def test_slack_verify_clean_reports_residue_from_a_fresh_instance() -> None:
    fake = FakeSlack()
    seeder = slack_app(fake)
    manifest = SeedManifest(scenario_id="unit")
    await seeder.seed(SLACK_SEED, manifest)
    await seeder.aclose()

    checker = slack_app(fake)
    checker.remember(manifest)
    residue = await checker.verify_clean()
    assert len(residue) == 3 and all(line.startswith("slack:#") for line in residue)

    await checker.reset(manifest)
    assert await checker.verify_clean() == []
    ops = manifest.aliases["slack"]["ops-desk"]
    fake.add_message(ops, "stray", ours=True)
    assert len(await checker.verify_clean()) == 1
    await checker.aclose()


async def test_slack_snapshot_lists_channel_histories_and_users() -> None:
    fake = FakeSlack()
    app = slack_app(fake)
    manifest = SeedManifest(scenario_id="unit")
    await app.seed(SLACK_SEED, manifest)
    snapshot = await app.snapshot()
    assert set(snapshot["channels"]) == {"ops-desk", "announcements"}
    ops = snapshot["channels"]["ops-desk"]
    assert ops["id"] == manifest.aliases["slack"]["ops-desk"]
    assert [m["username"] for m in ops["messages"]] == ["Ops Coordinator", "Marlon Vega"]
    assert set(ops["messages"][0]) == {"ts", "user", "username", "text", "thread_ts", "subtype", "bot_id"}
    assert snapshot["users"][0]["name"] == "benchpress"
    await app.aclose()


async def test_slack_api_errors_raise_with_the_slack_error_code() -> None:
    fake = FakeSlack()
    fake.fail_with = "not_authed"
    app = slack_app(fake)
    with pytest.raises(SlackApiError, match="not_authed") as info:
        await app.snapshot()
    assert info.value.method == "conversations.list" and info.value.error == "not_authed"
    await app.aclose()


async def test_slack_real_host_requires_scratch_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BENCHPRESS_SCRATCH_OK", raising=False)
    fake = FakeSlack()
    app = slack_app(fake, base_url="https://slack.com")
    with pytest.raises(ScratchGuardError):
        await app.seed(SLACK_SEED, SeedManifest(scenario_id="unit"))
    assert fake.requests == []
    monkeypatch.setenv("BENCHPRESS_SCRATCH_OK", "1")
    assert (await app.seed(SLACK_SEED, SeedManifest(scenario_id="unit"))).counts["messages"] == 3
    await app.aclose()


def test_slack_from_env_prefers_a_devsim_twin() -> None:
    with pytest.raises(ValueError, match="SLACK_BOT_TOKEN"):
        SlackApp.from_env({})
    app = SlackApp.from_env({"SLACK_BOT_TOKEN": "xoxb-unit", "DEVSIM_SLACK_URL": "http://127.0.0.1:8081/"})
    assert app.base_url == "http://127.0.0.1:8081"


# -- fake Stripe ---------------------------------------------------------------------------------------


class FakeStripe:
    def __init__(self) -> None:
        self.customers: dict[str, dict[str, Any]] = {}
        self.products: dict[str, dict[str, Any]] = {}
        self.prices: dict[str, dict[str, Any]] = {}
        self.requests: list[httpx.Request] = []
        self.clock = int(time.time())
        self._seq = 0

    def _id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}_{self._seq:04d}"

    def add_customer(self, name: str, email: str, *, created: int | None = None) -> str:
        customer_id = self._id("cus")
        self.customers[customer_id] = {
            "id": customer_id,
            "object": "customer",
            "name": name,
            "email": email,
            "description": None,
            "metadata": {},
            "created": self.clock if created is None else created,
        }
        return customer_id

    def add_product(self, name: str, *, with_price: bool) -> str:
        product_id = self._id("prod")
        self.products[product_id] = {
            "id": product_id,
            "object": "product",
            "name": name,
            "active": True,
            "created": self.clock,
        }
        if with_price:
            price_id = self._id("price")
            self.prices[price_id] = {
                "id": price_id,
                "object": "price",
                "product": product_id,
                "currency": "usd",
                "unit_amount": 100,
                "active": True,
            }
        return product_id

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        params = dict(parse_qsl(request.url.query.decode(), keep_blank_values=True))
        form = dict(parse_qsl(request.content.decode(), keep_blank_values=True)) if request.content else {}
        if request.method == "GET" and path in {"/v1/customers", "/v1/products", "/v1/prices"}:
            store = {"/v1/customers": self.customers, "/v1/products": self.products, "/v1/prices": self.prices}[path]
            items = list(store.values())
            if "created[gte]" in params:
                items = [item for item in items if int(item["created"]) >= int(params["created[gte]"])]
            if "product" in params:
                items = [item for item in items if item["product"] == params["product"]]
            if "active" in params:
                items = [item for item in items if item["active"] is (params["active"] == "true")]
            if "starting_after" in params:
                ids = [item["id"] for item in items]
                items = items[ids.index(params["starting_after"]) + 1 :]
            limit = int(params.get("limit", "10"))
            page = items[:limit]
            return httpx.Response(200, json={"object": "list", "data": page, "has_more": len(items) > limit})
        if request.method == "POST" and path == "/v1/customers":
            customer_id = self.add_customer(form["name"], form["email"])
            return httpx.Response(200, json=self.customers[customer_id])
        if request.method == "POST" and path == "/v1/products":
            product_id = self.add_product(form["name"], with_price=False)
            return httpx.Response(200, json=self.products[product_id])
        if request.method == "POST" and path == "/v1/prices":
            price_id = self._id("price")
            self.prices[price_id] = {
                "id": price_id,
                "object": "price",
                "product": form["product"],
                "currency": form["currency"],
                "unit_amount": int(form["unit_amount"]),
                "active": True,
            }
            return httpx.Response(200, json=self.prices[price_id])
        if request.method == "DELETE" and path.startswith("/v1/customers/"):
            customer_id = path.rsplit("/", 1)[1]
            if customer_id not in self.customers:
                return httpx.Response(
                    404,
                    json={
                        "error": {
                            "type": "invalid_request_error",
                            "code": "resource_missing",
                            "message": "No such customer",
                        }
                    },
                )
            del self.customers[customer_id]
            return httpx.Response(200, json={"id": customer_id, "object": "customer", "deleted": True})
        return httpx.Response(
            404, json={"error": {"type": "invalid_request_error", "message": f"Unrecognized request URL ({path})"}}
        )


def stripe_app(fake: FakeStripe) -> StripeApp:
    client = MockRealAppClient("http://127.0.0.1:2", {"Authorization": "Bearer sk_test_unit"}, fake)
    return StripeApp("sk_test_unit", base_url="http://127.0.0.1:2", client=client)


def _stripe_calls(fake: FakeStripe, method: str, path_prefix: str) -> list[httpx.Request]:
    return [r for r in fake.requests if r.method == method and r.url.path.startswith(path_prefix)]


# -- Stripe tests ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["sk_live_0123", "rk_test_0123", "pk_test_0123", ""])
def test_stripe_refuses_anything_but_test_secret_keys(key: str) -> None:
    with pytest.raises(ScratchGuardError):
        StripeApp(key)


async def test_stripe_seed_creates_customers_with_form_bodies_and_upserts_products() -> None:
    fake = FakeStripe()
    alpha_id = fake.add_product("Alpha Plan", with_price=True)
    app = stripe_app(fake)
    manifest = SeedManifest(scenario_id="unit")

    result = await app.seed(STRIPE_SEED, manifest)

    assert result.counts == {"customers": 2, "products": 2}
    assert any("products created: 1" in note for note in result.notes)
    assert any("prices created: 1" in note for note in result.notes)
    customer_posts = _stripe_calls(fake, "POST", "/v1/customers")
    assert len(customer_posts) == 2
    assert customer_posts[0].headers["content-type"] == "application/x-www-form-urlencoded"
    assert customer_posts[0].content.decode() == "name=Rivermill+Studio&email=billing%40rivermill.example"
    assert customer_posts[0].headers["authorization"] == "Bearer sk_test_unit"
    product_posts = _stripe_calls(fake, "POST", "/v1/products")
    assert len(product_posts) == 1 and dict(parse_qsl(product_posts[0].content.decode())) == {"name": "Beta Plan"}
    price_posts = _stripe_calls(fake, "POST", "/v1/prices")
    beta_id = next(pid for pid, p in fake.products.items() if p["name"] == "Beta Plan")
    assert len(price_posts) == 1
    assert dict(parse_qsl(price_posts[0].content.decode())) == {
        "product": beta_id,
        "currency": "usd",
        "unit_amount": "9900",
    }
    assert manifest.ids("stripe", "customers") == list(fake.customers)
    assert manifest.aliases["stripe"]["product:Alpha Plan"] == alpha_id
    assert manifest.aliases["stripe"]["customer:Rivermill Studio"] == manifest.ids("stripe", "customers")[0]
    assert "products" not in manifest.created.get("stripe", {}), "products are upserted, never reset"
    await app.aclose()


async def test_stripe_snapshot_paginates_customers_with_limit_and_starting_after() -> None:
    fake = FakeStripe()
    for index in range(150):
        fake.add_customer(f"Customer {index}", f"c{index}@rivermill.example")
    fake.add_product("Alpha Plan", with_price=True)
    app = stripe_app(fake)
    snapshot = await app.snapshot()
    assert len(snapshot["customers"]) == 150
    assert set(snapshot["customers"][0]) == {"id", "name", "email", "description", "metadata", "created"}
    assert len(snapshot["products"]) == 1 and len(snapshot["prices"]) == 1
    pages = _stripe_calls(fake, "GET", "/v1/customers")
    assert len(pages) == 2
    assert dict(parse_qsl(pages[0].url.query.decode())) == {"limit": "100"}
    assert dict(parse_qsl(pages[1].url.query.decode())) == {
        "limit": "100",
        "starting_after": snapshot["customers"][99]["id"],
    }
    await app.aclose()


async def test_stripe_reset_deletes_manifest_and_newer_customers_but_not_older_ones() -> None:
    fake = FakeStripe()
    older = fake.add_customer("Pre-existing", "old@rivermill.example", created=fake.clock - 600)
    app = stripe_app(fake)
    manifest = SeedManifest(scenario_id="unit", seeded_at=float(fake.clock))
    await app.seed(STRIPE_SEED, manifest)
    seeded = manifest.ids("stripe", "customers")
    stray = fake.add_customer("Agent Duplicate", "dup@rivermill.example", created=fake.clock + 30)

    await app.reset(manifest)

    assert set(fake.customers) == {older}
    deletes = [r.url.path.rsplit("/", 1)[1] for r in _stripe_calls(fake, "DELETE", "/v1/customers/")]
    assert deletes == [*seeded, stray]
    listing = _stripe_calls(fake, "GET", "/v1/customers")[-1]
    assert dict(parse_qsl(listing.url.query.decode()))["created[gte]"] == str(fake.clock - 5)
    assert await app.verify_clean() == []
    await app.aclose()


async def test_stripe_reset_tolerates_already_deleted_customers() -> None:
    fake = FakeStripe()
    app = stripe_app(fake)
    manifest = SeedManifest(scenario_id="unit", seeded_at=float(fake.clock))
    await app.seed(STRIPE_SEED, manifest)
    fake.customers.clear()
    await app.reset(manifest)
    assert len(_stripe_calls(fake, "DELETE", "/v1/customers/")) == 2
    await app.aclose()


async def test_stripe_verify_clean_reports_customers_created_since_the_seed() -> None:
    fake = FakeStripe()
    fake.add_customer("Pre-existing", "old@rivermill.example", created=fake.clock - 600)
    seeder = stripe_app(fake)
    manifest = SeedManifest(scenario_id="unit", seeded_at=float(fake.clock))
    await seeder.seed(STRIPE_SEED, manifest)
    residue = await seeder.verify_clean()
    assert len(residue) == 2 and all(line.startswith("stripe:customer cus_") for line in residue)
    await seeder.aclose()

    checker = stripe_app(fake)
    assert len(await checker.verify_clean()) == 3, "without a seed timestamp every customer is residue"
    checker.remember(manifest)
    assert len(await checker.verify_clean()) == 2
    await checker.aclose()


def test_stripe_from_env_requires_the_key_and_honours_the_twin_url() -> None:
    with pytest.raises(ValueError, match="STRIPE_SECRET_KEY"):
        StripeApp.from_env({})
    with pytest.raises(ScratchGuardError):
        StripeApp.from_env({"STRIPE_SECRET_KEY": "sk_live_x"})
    app = StripeApp.from_env({"STRIPE_SECRET_KEY": "sk_test_x", "DEVSIM_STRIPE_URL": "http://127.0.0.1:8082"})
    assert app.base_url == "http://127.0.0.1:8082"


# -- seed CLI helpers -------------------------------------------------------------------------------------


def test_expected_counts_cover_every_seeded_list() -> None:
    counts = expected_counts({**SLACK_SEED, **STRIPE_SEED, "hubspot": {"companies": [{}, {}], "settings": {"x": 1}}})
    assert counts == {
        "slack": {"channels": 2, "users": 2, "messages": 3},
        "stripe": {"customers": 2, "products": 2, "meters": 0},
        "hubspot": {"companies": 2},
    }


def test_format_counts_flags_mismatches() -> None:
    result = SeedResult(app="slack", counts={"channels": 2, "messages": 2})
    line, ok = format_counts(result, {"channels": 2, "messages": 3})
    assert line == "channels=2/2 messages=2/3" and ok is False
    line, ok = format_counts(SeedResult(app="stripe", counts={"customers": 4, "prices": 3}), {"customers": 4})
    assert line == "customers=4/4 prices=3" and ok is True


def test_build_apps_instantiates_known_drivers_and_skips_missing_modules() -> None:
    env = {
        "SLACK_BOT_TOKEN": "xoxb-unit",
        "STRIPE_SECRET_KEY": "sk_test_unit",
        "DEVSIM_SLACK_URL": "http://127.0.0.1:8081",
    }
    apps, skipped = build_apps(["slack", "stripe", "nonesuch"], env)
    assert set(apps) == {"slack", "stripe"}
    assert isinstance(apps["slack"], SlackApp) and isinstance(apps["stripe"], StripeApp)
    assert skipped == ["nonesuch (no evals.realapps.nonesuch module)"]


def test_load_task_seed_config_reads_the_vendored_scenario(tmp_path: Path) -> None:
    (tmp_path / "unit-01.json").write_text(json.dumps({"name": "unit", "seed_config": {**SLACK_SEED, **STRIPE_SEED}}))
    scenario_id, seed_config = load_task_seed_config("UNIT-01", tmp_path)
    assert scenario_id == "UNIT-01" and set(seed_config) == {"slack", "stripe"}
    with pytest.raises(FileNotFoundError):
        load_task_seed_config("UNIT-02", tmp_path)


async def test_seed_cli_end_to_end_seed_then_reset_verify_prints_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The acceptance flow: seed prints counts equal to the seed config; --reset --verify prints clean."""
    (tmp_path / "unit-01.json").write_text(json.dumps({"seed_config": {**SLACK_SEED, **STRIPE_SEED}}))
    fake_slack, fake_stripe = FakeSlack(), FakeStripe()
    fake_slack.add_channel("ops-desk")
    fake_stripe.add_product("Alpha Plan", with_price=True)

    def fake_build(names: Any, env: Any) -> tuple[dict[str, Any], list[str]]:
        apps: dict[str, Any] = {"slack": slack_app(fake_slack), "stripe": stripe_app(fake_stripe)}
        return {name: apps[name] for name in names if name in apps}, [n for n in names if n not in apps]

    monkeypatch.setattr(seed_cli, "SCENARIO_DIR", tmp_path)
    monkeypatch.setattr(seed_cli, "build_apps", fake_build)
    manifest = tmp_path / "runs" / "seed-manifest.json"
    snapshot = tmp_path / "state.json"

    code = await seed_cli.run(
        seed_cli.parse_args(
            [
                "--task",
                "UNIT-01",
                "--apps",
                "slack,stripe,hubspot",
                "--manifest",
                str(manifest),
                "--snapshot",
                str(snapshot),
            ]
        )
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "skipped:hubspot" in out
    assert "seeded:slack channels=2/2 messages=3/3" in out
    assert "seeded:stripe customers=2/2 products=2/2" in out
    assert "MISMATCH" not in out and f"manifest:{manifest}" in out and f"snapshot:{snapshot}" in out
    loaded = SeedManifest.load(manifest)
    assert loaded.scenario_id == "UNIT-01" and loaded.counts() == {
        "slack": {"channels": 1, "messages": 3},
        "stripe": {"customers": 2},
    }
    state = json.loads(snapshot.read_text())
    assert len(state["slack"]["channels"]["ops-desk"]["messages"]) == 2 and len(state["stripe"]["customers"]) == 2

    code = await seed_cli.run(
        seed_cli.parse_args(
            ["--task", "UNIT-01", "--apps", "slack,stripe", "--manifest", str(manifest), "--reset", "--verify"]
        )
    )
    out = capsys.readouterr().out
    assert code == 0 and out.rstrip().endswith("clean")
    assert "reset:slack" in out and "reset:stripe" in out
    assert fake_slack.bot_messages(loaded.aliases["slack"]["ops-desk"]) == [] and fake_stripe.customers == {}

    fake_stripe.add_customer("Stray", "stray@rivermill.example")
    code = await seed_cli.run(
        seed_cli.parse_args(["--task", "UNIT-01", "--apps", "stripe", "--manifest", str(manifest), "--verify"])
    )
    out = capsys.readouterr().out
    assert code == 1 and "residue:stripe:customer" in out and "clean" not in out
