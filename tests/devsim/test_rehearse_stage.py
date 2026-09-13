"""The devsim rehearsal stage: fresh, isolated, in-process twins behind the real gateway."""

from __future__ import annotations

from typing import Any, cast

from benchpress.rehearse import DEFAULT_NORMALIZER, state_hash
from devsim.rehearse_stage import TwinStage, twin_stage_factory

SEED: dict[str, Any] = {
    "stripe": {
        "customers": [
            {"name": "Example Studio", "email": "billing@studio.example"},
            {"name": "Example Studios Lead", "email": "hello@studios-lead.example"},
        ]
    }
}


def _customer_id(stage: TwinStage, email: str) -> str:
    customers = cast(dict[str, dict[str, Any]], stage.stores["stripe"].admin_state()["customers"])
    return next(cid for cid, customer in customers.items() if customer["email"] == email)


async def test_stages_are_fresh_isolated_and_identically_seeded() -> None:
    factory = twin_stage_factory(["stripe"], SEED)
    first = cast(TwinStage, await factory())
    second = cast(TwinStage, await factory())
    try:
        baseline = await first.snapshot()
        assert baseline == await second.snapshot()
        cid = _customer_id(first, "billing@studio.example")
        path = f"/v1/customers/{cid}"
        write = {"provider": "stripe", "method": "POST", "path": path, "body": {"email": "ap@studio.example"}}
        result = await first.execute_tool("provider_api", write)
        assert cast(dict[str, Any], result)["status_code"] == 200
        read = cast(
            dict[str, Any],
            await first.execute_tool("provider_api", {"provider": "stripe", "method": "GET", "path": path}),
        )
        assert read["body"]["email"] == "ap@studio.example"
        assert (await second.snapshot()) == baseline, "a write on one stage never leaks into another"
        assert state_hash(DEFAULT_NORMALIZER.normalize(await first.snapshot(), {})) != state_hash(
            DEFAULT_NORMALIZER.normalize(baseline, {})
        )
    finally:
        await first.aclose()
        await second.aclose()


async def test_control_plane_and_unknown_hosts_never_reach_a_twin() -> None:
    stage = TwinStage(["stripe"], SEED)
    try:
        blocked = cast(
            dict[str, Any],
            await stage.execute_tool("provider_api", {"provider": "stripe", "method": "GET", "path": "/admin/state"}),
        )
        assert blocked.get("status_code") != 200
    finally:
        await stage.aclose()


async def test_mutate_hook_changes_only_that_stage() -> None:
    def tamper(stores: Any) -> None:
        customers = stores["stripe"].customers
        next(iter(customers.values()))["name"] = "Changed Out Of Band"

    changed = TwinStage(["stripe"], SEED, mutate=tamper)
    clean = TwinStage(["stripe"], SEED)
    try:
        assert (await changed.snapshot()) != (await clean.snapshot())
    finally:
        await changed.aclose()
        await clean.aclose()
