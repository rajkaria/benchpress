"""VerifiedWrite: gate, execute, read back, evidence. No model, no controller."""

from __future__ import annotations

from typing import Any

from benchpress.context import Candidate, Context, ReadBack
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
