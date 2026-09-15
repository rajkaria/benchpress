"""One canonical JSON line per write: the unit of parity between library, HTTP and MCP."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from benchpress.context import Context, ReadBack
from benchpress.schemas import validate_write_line, write_schema
from benchpress.verified import VerifiedWrite
from benchpress.write_receipts import WRITE_PROTOCOL, JsonlReceiptSink, MemoryReceiptSink, write_line
from tests.conftest import make_action

_PROMPT = "Rivermill Studio asked for renewal notices to go to ap@rivermill.example."
FIXED = "2026-09-14T00:00:00.000Z"


async def _provider(tool_name: str, tool_input: dict[str, Any]) -> object:
    if tool_input["method"] == "GET":
        return {"status_code": 200, "body": {"id": "701", "email": "ap@rivermill.example"}}
    return {"status_code": 200, "body": {"id": "701"}}


def _action():
    return make_action(
        "w1",
        body={"properties": {"email": "ap@rivermill.example"}},
        fields=("email",),
        readback=ReadBack(path="/crm/v3/objects/companies/701", field_path="email"),
    )


async def test_run_emits_one_canonical_line_per_write() -> None:
    sink = MemoryReceiptSink()
    writer = VerifiedWrite(
        _provider, context=Context(user_prompt=_PROMPT), receipts=sink, clock=lambda: FIXED, session="s1"
    )
    outcome = await writer.run(_action())
    assert len(sink.lines) == 1
    line = sink.lines[0]
    assert line == write_line(outcome, at=FIXED, workspace="local", session="s1")
    payload = json.loads(line)
    assert list(payload) == sorted(payload)
    assert payload["protocol"] == WRITE_PROTOCOL and payload["event"] == "write"
    assert payload["status"] == "verified" and payload["status_code"] == 200
    assert payload["approval"] is None
    validate_write_line(line)


async def test_refusals_are_receipted_too() -> None:
    sink = MemoryReceiptSink()
    writer = VerifiedWrite(_provider, context=Context(user_prompt=_PROMPT), receipts=sink, clock=lambda: FIXED)
    await writer.run(_action())
    await writer.run(_action())
    second = json.loads(sink.lines[1])
    assert second["status"] == "refused" and second["verdict"]["rule"] == "idempotency"
    assert second["session"] == "local"  # Context().trial_id default


async def test_jsonl_sink_appends_lines(tmp_path: Path) -> None:
    path = tmp_path / "receipts" / "writes.jsonl"
    sink = JsonlReceiptSink(path)
    await sink.emit('{"a": 1}')
    await sink.emit('{"b": 2}')
    assert path.read_text(encoding="utf-8") == '{"a": 1}\n{"b": 2}\n'


def test_write_schema_is_a_fresh_copy() -> None:
    schema = write_schema()
    schema["title"] = "mutated"
    assert write_schema()["title"] != "mutated"
