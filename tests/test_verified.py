"""VerifiedWrite: gate, execute, read back, evidence. No model, no controller."""

from __future__ import annotations

import asyncio
from typing import Any

from benchpress.context import Candidate, Context, ReadBack
from benchpress.tools import MAX_PROVIDER_CALLS
from benchpress.verified import VerifiedWrite
from tests.conftest import make_action

_PROMPT = "Rivermill Studio asked for renewal notices to go to ap@rivermill.example."


class FakeProvider:
    """An in-memory record store that answers provider_api calls the way the tool bus sends them."""

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {"701": {"id": "701", "email": "old@rivermill.example"}}
        self.calls: list[dict[str, Any]] = []

    async def execute_tool(self, tool_name: str, tool_input: dict[str, Any]) -> object:
        self.calls.append(dict(tool_input))
        method, path = tool_input["method"], tool_input["path"]
        record_id = path.rstrip("/").split("/")[-1]
        if method == "GET":
            rec = self.records.get(record_id)
            return {"status_code": 200 if rec else 404, "body": rec or {"error": "not found"}}
        body: dict[str, Any] = tool_input.get("body") or {}
        props: dict[str, Any] = body.get("properties", body)
        self.records.setdefault(record_id, {"id": record_id}).update(props)
        return {"status_code": 200, "body": self.records[record_id]}


def _write(record: str = "701", email: str = "ap@rivermill.example", *, readback: bool = True):
    return make_action(
        "w1",
        path=f"/crm/v3/objects/companies/{record}",
        body={"properties": {"email": email}},
        fields=("email",),
        readback=ReadBack(path=f"/crm/v3/objects/companies/{record}", field_path="email") if readback else None,
    )


async def test_verified_when_the_readback_matches() -> None:
    provider = FakeProvider()
    outcome = await VerifiedWrite(provider.execute_tool, context=Context(user_prompt=_PROMPT)).run(_write())
    assert outcome.status == "verified"
    assert outcome.verdict.allowed
    assert [e.match for e in outcome.evidence] == [True]
    assert [c["method"] for c in provider.calls] == ["PATCH", "GET"]


async def test_mismatch_when_the_provider_lies() -> None:
    async def lying(tool_name: str, tool_input: dict[str, Any]) -> object:
        if tool_input["method"] == "GET":
            return {"status_code": 200, "body": {"id": "701", "email": "old@rivermill.example"}}
        return {"status_code": 200, "body": {"id": "701"}}

    outcome = await VerifiedWrite(lying, context=Context(user_prompt=_PROMPT)).run(_write())
    assert outcome.status == "mismatch"
    assert outcome.evidence[0].expected == "ap@rivermill.example"
    assert outcome.evidence[0].observed == "old@rivermill.example"


async def test_refused_write_never_reaches_the_executor() -> None:
    provider = FakeProvider()
    ctx = Context(user_prompt=_PROMPT)
    ctx.protected.add_candidate(
        Candidate(provider="hubspot", resource_type="company", resource_id="702", display="Rivermill Studio Prospect")
    )
    outcome = await VerifiedWrite(provider.execute_tool, context=ctx).run(_write(record="702"))
    assert outcome.status == "refused"
    assert outcome.verdict.rule == "protected"
    assert provider.calls == []


async def test_failed_when_the_provider_errors() -> None:
    async def broken(tool_name: str, tool_input: dict[str, Any]) -> object:
        return {"status_code": 500, "body": {"error": "boom"}}

    outcome = await VerifiedWrite(broken, context=Context(user_prompt=_PROMPT)).run(_write())
    assert outcome.status == "failed"
    assert outcome.evidence == ()


async def test_unverified_when_no_readback_is_declared() -> None:
    provider = FakeProvider()
    outcome = await VerifiedWrite(provider.execute_tool, context=Context(user_prompt=_PROMPT)).run(
        _write(readback=False)
    )
    assert outcome.status == "unverified"
    assert [c["method"] for c in provider.calls] == ["PATCH"]


async def test_a_second_identical_write_is_refused_as_idempotent() -> None:
    provider = FakeProvider()
    writer = VerifiedWrite(provider.execute_tool, context=Context(user_prompt=_PROMPT))
    action = _write()

    first = await writer.run(action)
    assert first.status == "verified"

    second = await writer.run(action)
    assert second.status == "refused"
    assert second.verdict.rule == "idempotency"
    assert [c["method"] for c in provider.calls] == ["PATCH", "GET"]


async def test_one_instance_verifies_many_sequential_writes() -> None:
    """A long-lived instance is not a benchmark trial: no per-phase budget, no lifetime cap."""
    provider = FakeProvider()
    writer = VerifiedWrite(provider.execute_tool, context=Context(user_prompt=_PROMPT))
    statuses = [
        (await writer.run(_write(record=str(800 + n), email=f"ap{n}@rivermill.example"))).status for n in range(10)
    ]
    assert statuses == ["verified"] * 10
    assert len(provider.calls) == 20


async def test_run_never_raises_past_the_benchmark_lifetime_cap() -> None:
    provider = FakeProvider()
    writer = VerifiedWrite(provider.execute_tool, context=Context(user_prompt=_PROMPT))
    for n in range(MAX_PROVIDER_CALLS // 2 + 1):
        outcome = await writer.run(_write(record=str(1000 + n), email=f"ap{n}@rivermill.example"))
        assert outcome.status == "verified", n
    assert len(provider.calls) > MAX_PROVIDER_CALLS


async def test_a_skipped_readback_is_unverified_with_a_reason() -> None:
    provider = FakeProvider()
    writer = VerifiedWrite(provider.execute_tool, context=Context(user_prompt=_PROMPT))
    bus = writer._bus  # pyright: ignore[reportPrivateUsage]
    # Force the one path that can skip a read-back: a bounded bus whose last call is the write itself.
    bus.budgets = True
    bus.enter("P7")
    bus.provider_calls = MAX_PROVIDER_CALLS - 1
    outcome = await writer.run(_write())
    assert outcome.status == "unverified"
    assert [c["method"] for c in provider.calls] == ["PATCH"]
    assert len(outcome.evidence) == 1
    assert outcome.evidence[0].match is False
    assert "skipped" in outcome.evidence[0].observed
    assert "budget" in outcome.evidence[0].observed


async def test_a_404_readback_is_unverified_not_mismatch() -> None:
    async def vanishing(tool_name: str, tool_input: dict[str, Any]) -> object:
        if tool_input["method"] == "GET":
            return {"status_code": 404, "body": {"error": "not found"}}
        return {"status_code": 200, "body": {"id": "701"}}

    outcome = await VerifiedWrite(vanishing, context=Context(user_prompt=_PROMPT)).run(_write())
    assert outcome.status == "unverified"
    assert [(e.expected, e.match) for e in outcome.evidence] == [("readable", False)]
    assert outcome.evidence[0].observed.startswith("404")


async def test_a_raising_readback_is_unverified_not_mismatch() -> None:
    async def flaky(tool_name: str, tool_input: dict[str, Any]) -> object:
        if tool_input["method"] == "GET":
            raise ConnectionError("read timed out")
        return {"status_code": 200, "body": {"id": "701"}}

    outcome = await VerifiedWrite(flaky, context=Context(user_prompt=_PROMPT)).run(_write())
    assert outcome.status == "unverified"
    assert [(e.expected, e.match) for e in outcome.evidence] == [("readable", False)]
    assert "read timed out" in outcome.evidence[0].observed


async def test_concurrent_identical_writes_send_one_request() -> None:
    provider = FakeProvider()

    async def slow(tool_name: str, tool_input: dict[str, Any]) -> object:
        await asyncio.sleep(0.01)
        return await provider.execute_tool(tool_name, tool_input)

    writer = VerifiedWrite(slow, context=Context(user_prompt=_PROMPT))
    first, second = await asyncio.gather(writer.run(_write()), writer.run(_write()))
    assert sorted([first.status, second.status]) == ["refused", "verified"]
    refused = first if first.status == "refused" else second
    assert refused.verdict.rule == "idempotency"
    assert [c["method"] for c in provider.calls] == ["PATCH", "GET"]


async def test_concurrent_distinct_writes_are_not_serialized_into_refusals() -> None:
    provider = FakeProvider()

    async def slow(tool_name: str, tool_input: dict[str, Any]) -> object:
        await asyncio.sleep(0.01)
        return await provider.execute_tool(tool_name, tool_input)

    writer = VerifiedWrite(slow, context=Context(user_prompt=_PROMPT))
    outcomes = await asyncio.gather(*(writer.run(_write(record=str(900 + n))) for n in range(5)))
    assert [o.status for o in outcomes] == ["verified"] * 5


async def test_a_mismatched_write_is_still_recorded_as_done() -> None:
    """2xx then a contradicting read-back: the write happened, so replaying it is refused, not re-sent."""
    calls: list[str] = []

    async def lying(tool_name: str, tool_input: dict[str, Any]) -> object:
        calls.append(tool_input["method"])
        if tool_input["method"] == "GET":
            return {"status_code": 200, "body": {"id": "701", "email": "old@rivermill.example"}}
        return {"status_code": 200, "body": {"id": "701"}}

    writer = VerifiedWrite(lying, context=Context(user_prompt=_PROMPT))
    assert (await writer.run(_write())).status == "mismatch"
    replay = await writer.run(_write())
    assert replay.status == "refused" and replay.verdict.rule == "idempotency"
    assert calls == ["PATCH", "GET"]
