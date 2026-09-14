"""ToolBus budgets: enforced by default for the controller, switchable off for primitives that are not a trial."""

from __future__ import annotations

from typing import Any

import pytest

from benchpress.context import Context
from benchpress.gate import Gate
from benchpress.tools import MAX_PROVIDER_CALLS, PHASE_BUDGETS, BudgetExhausted, ToolBus


async def _ok(tool_name: str, tool_input: dict[str, Any]) -> object:
    return {"status_code": 200, "body": {"id": "1"}}


def _bus(**kwargs: Any) -> ToolBus:
    ctx = Context()
    return ToolBus(context=ctx, execute=_ok, gate=Gate(ctx), **kwargs)


async def test_default_bus_raises_at_the_phase_cap() -> None:
    bus = _bus()
    cap = PHASE_BUDGETS["P0"].provider_api
    for _ in range(cap):
        await bus.read("hubspot", "/crm/v3/objects/companies/1")
    with pytest.raises(BudgetExhausted):
        await bus.read("hubspot", "/crm/v3/objects/companies/1")
    assert bus.provider_calls == cap


async def test_default_bus_raises_at_the_lifetime_cap() -> None:
    bus = _bus()
    bus.enter("P7")
    bus.provider_calls = MAX_PROVIDER_CALLS
    with pytest.raises(BudgetExhausted):
        await bus.read("hubspot", "/crm/v3/objects/companies/1")


async def test_unbounded_bus_ignores_phase_and_lifetime_caps() -> None:
    bus = _bus(budgets=False)
    for _ in range(MAX_PROVIDER_CALLS + 5):
        result = await bus.read("hubspot", "/crm/v3/objects/companies/1")
        assert result.ok
    assert bus.provider_calls == MAX_PROVIDER_CALLS + 5
    assert bus.budget_left() > 0
    assert bus.budget_left(docs=True) > 0
