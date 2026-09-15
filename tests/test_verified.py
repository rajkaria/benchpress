"""VerifiedWrite: gate, execute, read back, evidence. No model, no controller."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from benchpress.context import Candidate, Context, ReadBack
from benchpress.idempotency import InMemoryIdempotencyStore
from benchpress.phases.execute import readback_evidence
from benchpress.tools import MAX_PROVIDER_CALLS, ToolResult
from benchpress.verified import VerifiedWrite
from benchpress.write_receipts import MemoryReceiptSink
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


async def test_a_declared_field_missing_from_the_readback_is_a_mismatch() -> None:
    """A 2xx read-back that never shows the written field must not count as verified."""

    async def forgetful(tool_name: str, tool_input: dict[str, Any]) -> object:
        if tool_input["method"] == "GET":
            return {"status_code": 200, "body": {"id": "701"}}
        return {"status_code": 200, "body": {"id": "701"}}

    outcome = await VerifiedWrite(forgetful, context=Context(user_prompt=_PROMPT)).run(_write())
    assert outcome.status == "mismatch"
    assert [(e.check, e.expected, e.observed, e.match) for e in outcome.evidence] == [
        ("readback:w1:email", "ap@rivermill.example", "missing", False)
    ]
    assert "email" in outcome.evidence[0].detail


def test_readback_evidence_reports_each_missing_declared_field() -> None:
    action = make_action(
        "w2",
        body={"properties": {"email": "ap@rivermill.example", "name": "Rivermill Studio"}},
        fields=("email", "name"),
        readback=ReadBack(path="/crm/v3/objects/companies/701", field_path="properties.email"),
    )
    read = ToolResult(ok=True, status_code=200, body={"properties": {"email": "ap@rivermill.example", "name": None}})
    evidence = readback_evidence(action, read)
    assert [(e.check, e.observed, e.match) for e in evidence] == [
        ("readback:w2:email", "ap@rivermill.example", True),
        ("readback:w2:name", "missing", False),
    ]


def test_a_field_the_readback_declares_unobserved_is_not_required() -> None:
    action = make_action(
        "m1",
        provider="slack",
        method="POST",
        path="/api/chat.postMessage",
        body={"channel": "C1", "text": "Renewal notices now go to AP."},
        fields=("channel", "text"),
        readback=ReadBack(
            path="/api/conversations.replies",
            query={"channel": "C1"},
            field_path="messages.0.text",
            unobserved=("channel",),
        ),
    )
    read = ToolResult(ok=True, status_code=200, body={"messages": [{"text": "Renewal notices now go to AP."}]})
    assert [(e.check, e.match) for e in readback_evidence(action, read)] == [("readback:m1:text", True)]


def test_a_sibling_of_the_readback_field_path_is_observed() -> None:
    action = make_action(
        "m2",
        provider="slack",
        method="POST",
        path="/api/chat.postMessage",
        body={"channel": "C1", "text": "Done.", "thread_ts": "1700000000.000100"},
        fields=("text", "thread_ts"),
        readback=ReadBack(path="/api/conversations.replies", field_path="messages.0.text"),
    )
    read = ToolResult(
        ok=True, status_code=200, body={"messages": [{"text": "Done.", "thread_ts": "1700000000.000100"}]}
    )
    assert [(e.check, e.match) for e in readback_evidence(action, read)] == [
        ("readback:m2:text", True),
        ("readback:m2:thread_ts", True),
    ]


def test_a_bracketed_form_field_is_observed_at_its_dotted_path() -> None:
    action = make_action(
        "s1",
        provider="stripe",
        method="POST",
        path="/v1/customers/cus_1",
        body={"metadata[lifecycle]": "customer"},
        fields=("metadata[lifecycle]",),
        readback=ReadBack(path="/v1/customers/cus_1", field_path="metadata.lifecycle"),
    )
    read = ToolResult(ok=True, status_code=200, body={"id": "cus_1", "metadata": {"lifecycle": "customer"}})
    assert [(e.check, e.observed, e.match) for e in readback_evidence(action, read)] == [
        ("readback:s1:metadata[lifecycle]", "customer", True)
    ]


async def test_two_instances_sharing_a_store_send_one_write() -> None:
    provider = FakeProvider()
    store = InMemoryIdempotencyStore()
    first = VerifiedWrite(provider.execute_tool, context=Context(user_prompt=_PROMPT), idempotency=store, scope="acct")
    second = VerifiedWrite(provider.execute_tool, context=Context(user_prompt=_PROMPT), idempotency=store, scope="acct")
    assert (await first.run(_write())).status == "verified"
    replay = await second.run(_write())
    assert replay.status == "refused" and replay.verdict.rule == "idempotency"
    assert "already succeeded" in replay.verdict.reason
    assert [c["method"] for c in provider.calls] == ["PATCH", "GET"]
    ctx = second.context
    assert [v.rule for v in ctx.refusals] == ["idempotency"]
    assert [d.verdict.rule for d in ctx.gate_decisions] == ["idempotency"]
    assert ctx.ledger[-1].error == "gate:idempotency"


async def test_instances_with_different_scopes_do_not_share_claims() -> None:
    provider = FakeProvider()
    store = InMemoryIdempotencyStore()
    a = VerifiedWrite(provider.execute_tool, context=Context(user_prompt=_PROMPT), idempotency=store, scope="a")
    b = VerifiedWrite(provider.execute_tool, context=Context(user_prompt=_PROMPT), idempotency=store, scope="b")
    assert (await a.run(_write())).status == "verified"
    assert (await b.run(_write(email="ap@rivermill.example"))).status == "verified"


async def test_an_in_flight_twin_on_another_instance_is_refused() -> None:
    provider = FakeProvider()
    store = InMemoryIdempotencyStore()
    release = asyncio.Event()

    async def slow(tool_name: str, tool_input: dict[str, Any]) -> object:
        if tool_input["method"] != "GET":
            await release.wait()
        return await provider.execute_tool(tool_name, tool_input)

    a = VerifiedWrite(slow, context=Context(user_prompt=_PROMPT), idempotency=store, scope="acct")
    b = VerifiedWrite(slow, context=Context(user_prompt=_PROMPT), idempotency=store, scope="acct")
    pending = asyncio.create_task(a.run(_write()))
    await asyncio.sleep(0)
    twin = await b.run(_write())
    release.set()
    assert (await pending).status == "verified"
    assert twin.status == "refused" and "in flight" in twin.verdict.reason


async def test_a_failed_write_releases_its_claim() -> None:
    attempts: list[str] = []

    async def flaky(tool_name: str, tool_input: dict[str, Any]) -> object:
        attempts.append(tool_input["method"])
        if len(attempts) == 1:
            return {"status_code": 503, "body": {"error": "busy"}}
        if tool_input["method"] == "GET":
            return {"status_code": 200, "body": {"id": "701", "email": "ap@rivermill.example"}}
        return {"status_code": 200, "body": {"id": "701"}}

    writer = VerifiedWrite(flaky, context=Context(user_prompt=_PROMPT))
    assert (await writer.run(_write())).status == "failed"
    assert (await writer.run(_write())).status == "verified"
    assert attempts == ["PATCH", "PATCH", "GET"]


async def test_a_replayed_write_is_refused_every_time_on_one_instance() -> None:
    """Single-instance repeats: each replay is caught by this instance's own `Gate` (`has_succeeded`), which
    routes through `perform`'s `except GateRefusal` branch (`already_recorded=True`) rather than the store's
    `refuse(..., already_recorded=False)` path. It does not by itself exercise the guard against deduplicating
    refusals by value equality — see `test_a_cross_instance_replay_is_refused_every_time_not_deduplicated` for
    that."""
    provider = FakeProvider()
    writer = VerifiedWrite(provider.execute_tool, context=Context(user_prompt=_PROMPT))
    action = _write()

    assert (await writer.run(action)).status == "verified"
    assert (await writer.run(action)).status == "refused"
    assert (await writer.run(action)).status == "refused"

    ctx = writer.context
    assert [v.rule for v in ctx.refusals] == ["idempotency", "idempotency"]
    assert [d.verdict.rule for d in ctx.gate_decisions if d.verdict.rule == "idempotency"] == [
        "idempotency",
        "idempotency",
    ]


async def test_a_cross_instance_replay_is_refused_every_time_not_deduplicated() -> None:
    """`ToolBus.refuse` appends every refusal it is asked to, never deduplicating by value equality.

    This is the path where the (rejected) `verdict not in context.refusals` guard would have failed: instance
    `b` never performs the write itself, so its own `Gate` never learns the fingerprint succeeded and keeps
    reporting the action as allowed. Every one of `b`'s replays therefore reaches the shared store's `claim`,
    finds `done`, and calls `self._bus.refuse(action, verdict)` with `already_recorded=False` — building an
    equal `GateVerdict` each time. A value-equality guard would only append the first of these.
    """
    provider = FakeProvider()
    store = InMemoryIdempotencyStore()
    a = VerifiedWrite(provider.execute_tool, context=Context(user_prompt=_PROMPT), idempotency=store, scope="acct")
    b = VerifiedWrite(provider.execute_tool, context=Context(user_prompt=_PROMPT), idempotency=store, scope="acct")
    assert (await a.run(_write())).status == "verified"

    first = await b.run(_write())
    second = await b.run(_write())
    assert first.status == "refused" and second.status == "refused"
    assert first.verdict.rule == "idempotency" and second.verdict.rule == "idempotency"

    ctx = b.context
    assert len(ctx.refusals) == len(ctx.gate_decisions) == 2
    assert [v.rule for v in ctx.refusals] == ["idempotency", "idempotency"]
    assert [d.verdict.rule for d in ctx.gate_decisions] == ["idempotency", "idempotency"]
    assert [c["method"] for c in provider.calls] == ["PATCH", "GET"]


async def test_evaluate_judges_an_action_without_executing_recording_or_claiming() -> None:
    provider = FakeProvider()
    ctx = Context(user_prompt=_PROMPT)
    ctx.protected.add_candidate(
        Candidate(provider="hubspot", resource_type="company", resource_id="702", display="Rivermill Studio Prospect")
    )
    writer = VerifiedWrite(provider.execute_tool, context=ctx, idempotency=InMemoryIdempotencyStore(), scope="acct")
    refused = writer.evaluate(_write(record="702"))
    assert (refused.allowed, refused.rule) == (False, "protected")
    assert writer.evaluate(_write()).allowed
    assert provider.calls == [] and ctx.refusals == [] and ctx.gate_decisions == []
    assert (await writer.run(_write())).status == "verified", "evaluate took no idempotency claim"
    assert writer.evaluate(_write()).rule == "idempotency"


async def test_history_limit_zero_keeps_no_per_write_records_and_changes_no_outcome() -> None:
    def build(history_limit: int | None) -> tuple[VerifiedWrite, MemoryReceiptSink]:
        ctx = Context(user_prompt=_PROMPT)
        prospect = Candidate(
            provider="hubspot", resource_type="company", resource_id="702", display="Rivermill Studio Prospect"
        )
        ctx.protected.add_candidate(prospect)
        sink = MemoryReceiptSink()
        writer = VerifiedWrite(
            FakeProvider().execute_tool,
            context=ctx,
            receipts=sink,
            clock=lambda: "2026-09-15T00:00:00.000Z",
            history_limit=history_limit,
        )
        return writer, sink

    actions = [_write(), _write(record="702"), _write(), _write(email="billing@rivermill.example")]
    trimmed, trimmed_sink = build(0)
    kept, kept_sink = build(None)
    trimmed_outcomes = [await trimmed.run(action) for action in actions]
    kept_outcomes = [await kept.run(action) for action in actions]

    assert [o.status for o in trimmed_outcomes] == ["verified", "refused", "refused", "verified"]
    assert [(o.status, o.verdict, o.evidence) for o in trimmed_outcomes] == [
        (o.status, o.verdict, o.evidence) for o in kept_outcomes
    ]
    assert all(o.evidence for o in trimmed_outcomes if o.status == "verified")
    assert trimmed_sink.lines == kept_sink.lines
    ctx = trimmed.context
    assert (ctx.gate_decisions, ctx.refusals, ctx.ledger, ctx.evidence) == ([], [], [], [])
    bus = trimmed._bus  # pyright: ignore[reportPrivateUsage]
    assert bus.events == () and bus.harness_events == ()
    kept_ctx = kept.context
    assert kept_ctx.gate_decisions and kept_ctx.refusals and kept_ctx.ledger and kept_ctx.evidence
    kept_bus = kept._bus  # pyright: ignore[reportPrivateUsage]
    assert kept_bus.events and kept_bus.harness_events
    with pytest.raises(ValueError, match="history_limit"):
        VerifiedWrite(FakeProvider().execute_tool, history_limit=-1)
