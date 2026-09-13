from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx
import pytest

from devsim.twins import PROVIDER_ROLES, available
from devsim.twins.base import Clock, Store, decode_form, det_alnum, det_digits, det_hex, make_admin_app


class _Toy(Store):
    provider = "toy"

    def __init__(self, seed_key: str) -> None:
        super().__init__(seed_key)
        self.things: dict[str, dict[str, Any]] = {}

    def seed(self, seed_config: Mapping[str, Any]) -> None:
        for item in seed_config.get("things", []):
            self.things[self.new_id("things", kind="alnum", prefix="thg_")] = dict(item)

    def admin_state(self) -> dict[str, Any]:
        return {"things": self.things}


def test_deterministic_ids_are_stable_and_distinct() -> None:
    assert det_hex("k", "c", 1) == det_hex("k", "c", 1)
    assert det_hex("k", "c", 1) != det_hex("k", "c", 2)
    assert det_hex("k", "c", 1) != det_hex("other", "c", 1)
    assert len(det_digits("k", "x", length=10)) == 10 and det_digits("k", "x")[0] != "0"
    assert det_alnum("k", "x", length=14, case="upper").isupper()


def test_clock_only_ticks_on_writes() -> None:
    store = _Toy("seed")
    before = store.clock.iso()
    store.seed({"things": [{"a": 1}]})
    assert store.clock.iso() == before, "seeding is not a write"
    store.record_mutation(method="POST", path="/things", collection="things", record_id="x", before=None, after={})
    assert store.clock.iso() > before
    assert store.journal[0].sequence == 1


def test_decode_form_handles_brackets() -> None:
    pairs = [("email", "a@b.example"), ("metadata[k]", "v"), ("items[0][price]", "p1"), ("items[1][price]", "p2")]
    decoded = decode_form(pairs)
    assert decoded == {"email": "a@b.example", "metadata": {"k": "v"}, "items": [{"price": "p1"}, {"price": "p2"}]}


@pytest.mark.asyncio
async def test_admin_app_serves_state_on_every_alias() -> None:
    store = _Toy("seed")
    store.seed({"things": [{"a": 1}]})
    app = make_admin_app(store)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://twin") as client:
        for path in ("/admin/state", "/_admin/state", "/inspect"):
            response = await client.get(path)
            assert response.status_code == 200
            assert list(response.json()["things"].values()) == [{"a": 1}]
        assert (await client.get("/healthz")).json()["provider"] == "toy"


def test_registry_lists_only_implemented_twins() -> None:
    assert set(available()) <= set(PROVIDER_ROLES)
    assert Clock().iso().endswith("Z")
