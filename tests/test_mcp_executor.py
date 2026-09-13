"""`benchpress.shims.mcp`: MCP sessions as a Benchpress ToolExecutor, against an in-memory MCP server.

No network, no model: the upstream is an in-process `MCPServer`, connected with `mcp.Client`.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from benchpress.context import Action, Context
from benchpress.gate import Gate
from benchpress.shims.mcp import McpToolRouter, ToolClass, mcp_executor
from benchpress.tools import ToolBus


def notes_server(store: dict[str, str]) -> MCPServer:
    """An upstream with a read tool, a write tool, a destructive tool and an unannotated tool."""
    server = MCPServer("notes")

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    def get_note(note_id: str) -> dict[str, str]:
        """Read one note."""
        if note_id not in store:
            raise ToolError(f"no note {note_id}")
        return {"id": note_id, "text": store[note_id]}

    @server.tool(annotations=ToolAnnotations(read_only_hint=True), structured_output=False)
    def list_notes() -> str:
        """List note ids as JSON text."""
        return '{"ids": [%s]}' % ", ".join(f'"{key}"' for key in sorted(store))  # noqa: UP031

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False))
    def upsert_note(note_id: str, text: str) -> dict[str, str]:
        """Create or replace a note."""
        store[note_id] = text
        return {"id": note_id, "text": text}

    @server.tool(annotations=ToolAnnotations(destructive_hint=True))
    def delete_note(note_id: str) -> str:
        """Delete a note."""
        store.pop(note_id, None)
        return "deleted"

    @server.tool()
    def archive_all() -> str:
        """Unannotated: the MCP default is non-read-only and destructive."""
        store.clear()
        return "archived"

    return server


def seeded() -> dict[str, str]:
    return {"n1": "first", "n2": "second"}


@asynccontextmanager
async def connected(
    store: dict[str, str], classes: dict[str, ToolClass] | None = None
) -> AsyncGenerator[McpToolRouter]:
    """The client connects inside the test's own task (anyio cancel scopes must exit where they were entered)."""
    async with Client(notes_server(store)) as client:
        yield McpToolRouter(sessions={"notes": client}, classes=classes or {})


async def api(router: McpToolRouter, method: str, path: str, **extra: object) -> dict[str, Any]:
    return await router.execute("provider_api", {"provider": "notes", "method": method, "path": path, **extra})


async def test_discovery_lists_tools_with_class_method_and_route() -> None:
    async with connected(seeded()) as router:
        listed = await api(router, "GET", "/tools")
        assert listed["ok"] and listed["status_code"] == 200
        tools = {tool["name"]: tool for tool in listed["body"]["tools"]}
        assert {name: (tool["class"], tool["method"]) for name, tool in tools.items()} == {
            "get_note": ("read", "GET"),
            "list_notes": ("read", "GET"),
            "upsert_note": ("write", "POST"),
            "delete_note": ("destructive", "DELETE"),
            "archive_all": ("destructive", "DELETE"),
        }
        assert tools["get_note"]["path"] == "/tools/get_note/call"
        assert tools["get_note"]["annotations"] == {"readOnlyHint": True}
        assert "note_id" in tools["get_note"]["input_schema"]["properties"]
        one = await api(router, "GET", "/tools/upsert_note")
        assert one["body"]["class"] == "write" and one["body"]["description"] == "Create or replace a note."


async def test_read_tool_via_get_with_body_or_query() -> None:
    async with connected(seeded()) as router:
        by_body = await api(router, "GET", "/tools/get_note/call", body={"note_id": "n1"})
        assert by_body["ok"] and by_body["body"] == {"id": "n1", "text": "first"}
        by_query = await api(router, "GET", "/tools/get_note/call", query={"note_id": "n2"})
        assert by_query["body"] == {"id": "n2", "text": "second"} and by_query["tool_class"] == "read"


async def test_a_get_can_never_reach_a_write_tool() -> None:
    store = seeded()
    async with connected(store) as router:
        attempts: list[tuple[str, dict[str, str]]] = [
            ("/tools/upsert_note/call", {"note_id": "n1", "text": "x"}),
            ("/tools/archive_all/call", {}),
        ]
        for path, body in attempts:
            refused = await api(router, "GET", path, body=body)
            assert refused["status_code"] == 405 and not refused["ok"]
            assert str(refused["error"]).startswith("method_mismatch")
        # nor can an ordinary write method reach a destructive tool
        assert (await api(router, "POST", "/tools/delete_note/call", body={"note_id": "n1"}))["status_code"] == 405
    assert store == seeded()


async def test_write_tool_via_post_then_read_back() -> None:
    store = seeded()
    async with connected(store) as router:
        written = await api(router, "POST", "/tools/upsert_note/call", body={"note_id": "n3", "text": "third"})
        assert written["ok"] and written["tool_class"] == "write"
        assert store["n3"] == "third"
        read_back = await api(router, "GET", "/tools/get_note/call", body={"note_id": "n3"})
        assert read_back["body"]["text"] == "third"


async def test_text_json_results_parse_and_tool_errors_are_422() -> None:
    async with connected(seeded()) as router:
        listed = await api(router, "GET", "/tools/list_notes/call")
        assert listed["body"] == {"ids": ["n1", "n2"]}
        missing = await api(router, "GET", "/tools/get_note/call", body={"note_id": "nope"})
        assert missing["status_code"] == 422 and not missing["ok"]
        assert "no note nope" in str(missing["error"])


async def test_unknown_provider_tool_route_and_bad_body() -> None:
    async with connected(seeded()) as router:
        other = await router.execute("provider_api", {"provider": "other", "method": "GET", "path": "/tools"})
        assert other["status_code"] == 404 and "notes" in str(other["error"])
        assert (await api(router, "GET", "/tools/nope/call"))["status_code"] == 404
        assert (await api(router, "GET", "/v1/customers"))["status_code"] == 404
        assert (await api(router, "POST", "/tools"))["status_code"] == 405
        bad_body = await api(router, "POST", "/tools/upsert_note/call", body=["not", "a", "mapping"])
        assert bad_body["status_code"] == 400
        assert (await router.execute("shell", {}))["ok"] is False


async def test_class_overrides_beat_annotations() -> None:
    store = seeded()
    async with connected(store, classes={"notes/archive_all": "write"}) as router:
        tools = await router.tools("notes")
        assert router.tool_class("notes", tools["archive_all"]) == "write"
        assert router.tool_class("notes", tools["delete_note"]) == "destructive"
        assert (await api(router, "POST", "/tools/archive_all/call"))["ok"]
    assert store == {}


async def test_docs_search_and_fetch() -> None:
    async with connected(seeded()) as router:
        found = await router.execute("provider_docs", {"action": "search", "query": "note"})
        names = {str(item["name"]) for item in found["results"]}
        assert found["ok"] and names >= {"get_note", "upsert_note", "delete_note"}
        fetched = await router.execute("provider_docs", {"action": "fetch", "provider": "notes", "tool": "delete_note"})
        assert fetched["tool"]["class"] == "destructive" and fetched["tool"]["method"] == "DELETE"
        missing = await router.execute("provider_docs", {"action": "fetch", "provider": "notes", "tool": "nope"})
        assert missing["ok"] is False


async def test_the_tool_bus_and_gate_drive_mcp_tools() -> None:
    """The loop's choke point: reads ungated, a write allowed once, a destructive tool refused in code."""
    store = seeded()
    async with Client(notes_server(store)) as client:
        ctx = Context(providers=("notes",))
        bus = ToolBus(context=ctx, execute=mcp_executor({"notes": client}), gate=Gate(ctx, allow_unplanned=True))
        bus.enter("P5")

        read = await bus.read("notes", "/tools/get_note/call", body={"note_id": "n1"})
        assert read.ok and read.json()["text"] == "first"

        write = Action(
            id="a1",
            kind="update",
            provider="notes",
            method="POST",
            path="/tools/upsert_note/call",
            body={"note_id": "n1", "text": "revised"},
            fields=("note_id", "text"),
            satisfies=("d1",),
        )
        result, verdict = await bus.perform(write)
        assert verdict.allowed and result.ok and store["n1"] == "revised"
        replay, replay_verdict = await bus.perform(write)
        assert not replay_verdict.allowed and replay_verdict.rule == "idempotency" and not replay.ok

        destroy = Action(
            id="a2",
            kind="update",
            provider="notes",
            method="DELETE",
            path="/tools/delete_note/call",
            body={"note_id": "n2"},
            satisfies=("d1",),
        )
        refused, refused_verdict = await bus.perform(destroy)
        assert not refused_verdict.allowed and refused_verdict.rule == "method" and not refused.ok

        failed = await bus.read("notes", "/tools/get_note/call", body={"note_id": "gone"})
        assert not failed.ok and failed.status_code == 422
        # refused calls never left the process: three executor calls, all provider_api
        assert [event["name"] for event in bus.harness_events] == ["provider_api"] * 3
    assert store == {"n1": "revised", "n2": "second"}


def test_import_benchpress_does_not_import_mcp() -> None:
    code = (
        "import sys, benchpress, benchpress.tools, benchpress.controller, benchpress.cli\nprint('mcp' in sys.modules)"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"
