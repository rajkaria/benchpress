"""`benchpress mcp-guard`: policy decisions, the proxy against an in-memory upstream, and the real stdio CLI.

No network and no model. The stdio test spawns two local Python processes (guard and upstream) over pipes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from mcp import Client, StdioServerParameters
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations
from pydantic import ValidationError

from benchpress import cli
from benchpress.shims import mcp_guard
from benchpress.shims.mcp_guard import Guard, GuardPolicy, GuardRule, arguments_digest, build_guard_server


def tool(name: str, *, read_only: bool | None = None, destructive: bool | None = None) -> Tool:
    annotated = read_only is not None or destructive is not None
    annotations = ToolAnnotations(read_only_hint=read_only, destructive_hint=destructive) if annotated else None
    return Tool(name=name, input_schema={"type": "object"}, annotations=annotations)


READ = tool("get_ticket", read_only=True)
WRITE = tool("update_ticket", destructive=False)
DESTROY = tool("delete_ticket", destructive=True)
BARE = tool("sync_everything")


# -- decisions ---------------------------------------------------------------------------


def test_reads_allowed_and_writes_refused_without_rules() -> None:
    guard = Guard(GuardPolicy())
    assert guard.decide("get_ticket", READ, {}).allowed
    write = guard.decide("update_ticket", WRITE, {})
    assert not write.allowed and write.rule == "no_allow_rule" and write.tool_class == "write"
    bare = guard.decide("sync_everything", BARE, {})
    assert not bare.allowed and bare.tool_class == "destructive"
    unknown = guard.decide("drop_tables", None, {})
    assert not unknown.allowed and unknown.rule == "unknown_tool"


def test_allow_rule_requires_every_constrained_argument_to_fully_match() -> None:
    rule = GuardRule(tool="update_*", arguments={"status": "pending|resolved", "ticket_id": r"T-\d+"})
    guard = Guard(GuardPolicy(rules=[rule]))
    assert guard.decide("update_ticket", WRITE, {"ticket_id": "T-12", "status": "resolved"}).allowed
    closed = guard.decide("update_ticket", WRITE, {"ticket_id": "T-12", "status": "resolved-and-closed"})
    assert not closed.allowed and closed.rule == "arguments_mismatch" and closed.policy_rule == 0
    missing = guard.decide("update_ticket", WRITE, {"status": "pending"})
    assert not missing.allowed and "ticket_id" in missing.reason
    numeric = Guard(GuardPolicy(rules=[GuardRule(tool="update_ticket", arguments={"priority": "[1-3]"})]))
    assert numeric.decide("update_ticket", WRITE, {"priority": 2}).allowed


def test_max_calls_counts_allowed_calls_per_rule() -> None:
    guard = Guard(GuardPolicy(rules=[GuardRule(tool="update_ticket", max_calls=2)]))
    decisions = [guard.decide("update_ticket", WRITE, {"n": n}) for n in range(3)]
    assert [d.allowed for d in decisions] == [True, True, False]
    assert decisions[2].rule == "max_calls"


def test_rules_apply_in_order_and_deny_wins_when_first() -> None:
    policy = GuardPolicy(
        rules=[GuardRule(tool="update_*", effect="deny", reason="tickets are frozen"), GuardRule(tool="*")]
    )
    refused = Guard(policy).decide("update_ticket", WRITE, {})
    assert not refused.allowed and refused.rule == "deny_rule" and refused.reason == "tickets are frozen"
    denied_read = Guard(GuardPolicy(rules=[GuardRule(tool="get_*", effect="deny")])).decide("get_ticket", READ, {})
    assert not denied_read.allowed and denied_read.rule == "deny_rule"


def test_destructive_tools_are_refused_even_by_a_wildcard_allow() -> None:
    wildcard = Guard(GuardPolicy(rules=[GuardRule(tool="*")]))
    assert wildcard.decide("update_ticket", WRITE, {}).allowed
    for name, spec in (("delete_ticket", DESTROY), ("sync_everything", BARE)):
        decision = wildcard.decide(name, spec, {})
        assert not decision.allowed and decision.rule == "destructive_default_deny"
    explicit = Guard(GuardPolicy(rules=[GuardRule(tool="delete_ticket", allow_destructive=True)]))
    assert explicit.decide("delete_ticket", DESTROY, {}).allowed


def test_class_overrides_and_read_denial() -> None:
    assert Guard(GuardPolicy(classes={"sync_everything": "read"})).decide("sync_everything", BARE, {}).allowed
    distrust = Guard(GuardPolicy(classes={"get_ticket": "write"})).decide("get_ticket", READ, {})
    assert not distrust.allowed and distrust.tool_class == "write"
    closed = Guard(GuardPolicy(reads="deny")).decide("get_ticket", READ, {})
    assert not closed.allowed and closed.rule == "reads_denied"


def test_policy_file_is_validated(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        GuardPolicy.model_validate({"rules": [{"tool": "x", "arguments": {"a": "("}}]})
    with pytest.raises(ValidationError):
        GuardPolicy.model_validate({"rules": [], "allow_everything": True})
    path = tmp_path / "guard.json"
    path.write_text(json.dumps({"rules": [{"tool": "update_*", "max_calls": 3}], "reads": "allow"}))
    assert GuardPolicy.load(path).rules[0].max_calls == 3


def test_receipt_lines_carry_a_digest_never_the_arguments(tmp_path: Path) -> None:
    receipts = tmp_path / "receipts.jsonl"
    guard = Guard(GuardPolicy(), receipts_path=receipts)
    arguments = {"ticket_id": "T-9", "note": "customer secret"}
    line = guard.record("update_ticket", arguments, guard.decide("update_ticket", WRITE, arguments), 0.0)
    assert line["decision"] == "refuse" and line["args_digest"] == arguments_digest(arguments)
    assert "customer secret" not in receipts.read_text()
    assert arguments_digest({"b": 1, "a": 2}) == arguments_digest({"a": 2, "b": 1})


def test_main_rejects_a_missing_command_or_policy(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    policy = tmp_path / "guard.json"
    policy.write_text("{}")
    assert mcp_guard.main(str(policy), ["--"]) == 2
    assert mcp_guard.main(str(tmp_path / "missing.json"), ["upstream"]) == 2
    assert "cannot load policy" in capsys.readouterr().err


def test_cli_passes_the_upstream_command_through_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_main(policy_path: str, upstream: list[str], receipts: str | None = None) -> int:
        seen.update(policy=policy_path, upstream=list(upstream), receipts=receipts)
        return 0

    monkeypatch.setattr(mcp_guard, "main", fake_main)
    argv = ["mcp-guard", "--policy", "g.json", "--receipts", "r.jsonl", "--", "npx", "-y", "server", "--flag"]
    assert cli.main(argv) == 0
    assert seen["policy"] == "g.json" and seen["receipts"] == "r.jsonl"
    assert [part for part in seen["upstream"] if part != "--"] == ["npx", "-y", "server", "--flag"]


# -- the proxy against an in-memory upstream ---------------------------------------------


def tickets_server(store: dict[str, str]) -> MCPServer:
    server = MCPServer("tickets")

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    def get_ticket(ticket_id: str) -> dict[str, str]:
        """Read a ticket."""
        return {"id": ticket_id, "status": store.get(ticket_id, "missing")}

    @server.tool(annotations=ToolAnnotations(destructive_hint=False))
    def update_ticket(ticket_id: str, status: str) -> dict[str, str]:
        """Set a ticket's status."""
        store[ticket_id] = status
        return {"id": ticket_id, "status": status}

    @server.tool(annotations=ToolAnnotations(destructive_hint=True))
    def delete_ticket(ticket_id: str) -> str:
        """Delete a ticket."""
        store.pop(ticket_id, None)
        return "deleted"

    return server


def text_of(result: CallToolResult) -> str:
    return "\n".join(block.text for block in result.content if isinstance(block, TextContent))


async def test_proxy_allows_refuses_and_writes_receipts(tmp_path: Path) -> None:
    store = {"T-1": "open", "T-2": "open"}
    receipts = tmp_path / "receipts.jsonl"
    policy = GuardPolicy(rules=[GuardRule(tool="update_ticket", arguments={"status": "pending|resolved"}, max_calls=1)])
    async with Client(tickets_server(store)) as upstream:
        server = build_guard_server(upstream.session, Guard(policy, receipts_path=receipts))
        async with Client(server) as client:
            listed = {t.name: t for t in (await client.session.list_tools()).tools}
            assert set(listed) == {"get_ticket", "update_ticket", "delete_ticket"}
            assert listed["get_ticket"].annotations is not None and listed["get_ticket"].annotations.read_only_hint

            read = await client.session.call_tool("get_ticket", {"ticket_id": "T-1"})
            assert not read.is_error and read.structured_content == {"id": "T-1", "status": "open"}

            mismatch = await client.session.call_tool("update_ticket", {"ticket_id": "T-1", "status": "closed"})
            assert mismatch.is_error and "[arguments_mismatch]" in text_of(mismatch)

            allowed = await client.session.call_tool("update_ticket", {"ticket_id": "T-1", "status": "resolved"})
            assert not allowed.is_error and allowed.structured_content == {"id": "T-1", "status": "resolved"}

            capped = await client.session.call_tool("update_ticket", {"ticket_id": "T-2", "status": "pending"})
            assert capped.is_error and "[max_calls]" in text_of(capped)

            destroyed = await client.session.call_tool("delete_ticket", {"ticket_id": "T-2"})
            assert destroyed.is_error and "[no_allow_rule]" in text_of(destroyed)

            unknown = await client.session.call_tool("drop_tables", {})
            assert unknown.is_error and "[unknown_tool]" in text_of(unknown)

    assert store == {"T-1": "resolved", "T-2": "open"}
    lines = [json.loads(raw) for raw in receipts.read_text().splitlines()]
    assert [(line["tool"], line["decision"], line["rule"]) for line in lines] == [
        ("get_ticket", "allow", "read"),
        ("update_ticket", "refuse", "arguments_mismatch"),
        ("update_ticket", "allow", "allow_rule"),
        ("update_ticket", "refuse", "max_calls"),
        ("delete_ticket", "refuse", "no_allow_rule"),
        ("drop_tables", "refuse", "unknown_tool"),
    ]
    assert lines[2]["args_digest"] == arguments_digest({"ticket_id": "T-1", "status": "resolved"})
    assert lines[2]["upstream_error"] is False and lines[1]["upstream_error"] is None
    assert all(isinstance(line["latency_ms"], (int, float)) and line["reason"] for line in lines)


# -- the real CLI over stdio -------------------------------------------------------------

UPSTREAM_SCRIPT = """
import json
import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

state = Path(sys.argv[1])
server = MCPServer("tickets")


def load():
    return json.loads(state.read_text())


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
def get_ticket(ticket_id: str) -> dict[str, str]:
    return {"id": ticket_id, "status": load().get(ticket_id, "missing")}


@server.tool(annotations=ToolAnnotations(destructive_hint=False))
def update_ticket(ticket_id: str, status: str) -> dict[str, str]:
    data = load()
    data[ticket_id] = status
    state.write_text(json.dumps(data))
    return {"id": ticket_id, "status": status}


@server.tool(annotations=ToolAnnotations(destructive_hint=True))
def delete_ticket(ticket_id: str) -> str:
    data = load()
    data.pop(ticket_id, None)
    state.write_text(json.dumps(data))
    return "deleted"


server.run()
"""


async def test_cli_stdio_proxy_end_to_end(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"T-1": "open"}))
    script = tmp_path / "upstream.py"
    script.write_text(UPSTREAM_SCRIPT)
    policy = tmp_path / "guard.json"
    policy.write_text(json.dumps({"rules": [{"tool": "update_ticket", "arguments": {"status": "resolved"}}]}))
    guard_args = ["-m", "benchpress.cli", "mcp-guard", "--policy", str(policy), "--"]
    params = StdioServerParameters(
        command=sys.executable, args=[*guard_args, sys.executable, str(script), str(state)], cwd=str(tmp_path)
    )
    async with Client(params, mode="legacy", read_timeout_seconds=60) as client:
        names = {t.name for t in (await client.session.list_tools()).tools}
        assert names == {"get_ticket", "update_ticket", "delete_ticket"}
        refused = await client.session.call_tool("delete_ticket", {"ticket_id": "T-1"})
        assert refused.is_error and "refused" in text_of(refused)
        allowed = await client.session.call_tool("update_ticket", {"ticket_id": "T-1", "status": "resolved"})
        assert not allowed.is_error
        read = await client.session.call_tool("get_ticket", {"ticket_id": "T-1"})
        assert read.structured_content == {"id": "T-1", "status": "resolved"}
    assert json.loads(state.read_text()) == {"T-1": "resolved"}
    receipts = [json.loads(raw) for raw in (tmp_path / "mcp-guard-receipts.jsonl").read_text().splitlines()]
    assert [(line["tool"], line["decision"]) for line in receipts] == [
        ("delete_ticket", "refuse"),
        ("update_ticket", "allow"),
        ("get_ticket", "allow"),
    ]
