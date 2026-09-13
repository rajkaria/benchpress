"""`benchpress.shims.openai_agents`: real SDK `FunctionTool`s invoked through the guarded path.

No network and no model API: tools are invoked through the SDK's own `invoke_function_tool`, and the
end-to-end test drives `Runner.run` with the SDK's `ScriptedModel`, tracing disabled.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest
from agents import Agent, FunctionTool, RunConfig, Runner, Tool, function_tool
from agents.items import ToolCallOutputItem
from agents.testing import ScriptedModel
from agents.testing.model import assistant_message, function_call
from agents.tool import invoke_function_tool
from agents.tool_context import ToolContext

from benchpress.packs import PolicyPack
from benchpress.shims.guard_policy import PolicyGuard
from benchpress.shims.openai_agents import (
    DEFAULT_RECEIPTS_NAME,
    AgentsToolGuard,
    GuardPolicy,
    GuardRule,
    ProviderCall,
    classify_tool_name,
    create_guard,
    guard_tools,
)


class Ledger:
    """Records which tool bodies actually ran."""

    def __init__(self) -> None:
        self.ran: list[tuple[str, dict[str, Any]]] = []


def make_tools(ledger: Ledger) -> list[FunctionTool]:
    @function_tool
    def get_ticket(ticket_id: str) -> str:
        """Read one ticket."""
        ledger.ran.append(("get_ticket", {"ticket_id": ticket_id}))
        return json.dumps({"id": ticket_id, "status": "open"})

    @function_tool
    def update_ticket(ticket_id: str, status: str) -> str:
        """Change a ticket's status."""
        ledger.ran.append(("update_ticket", {"ticket_id": ticket_id, "status": status}))
        return f"{ticket_id} -> {status}"

    @function_tool
    def delete_ticket(ticket_id: str) -> str:
        """Delete a ticket."""
        ledger.ran.append(("delete_ticket", {"ticket_id": ticket_id}))
        return f"{ticket_id} deleted"

    @function_tool
    def sync_everything() -> str:
        """Push local state everywhere."""
        ledger.ran.append(("sync_everything", {}))
        return "synced"

    return [get_ticket, update_ticket, delete_ticket, sync_everything]


def by_name(tools: list[FunctionTool]) -> dict[str, FunctionTool]:
    return {tool.name: tool for tool in tools}


async def invoke(tool: FunctionTool, arguments: dict[str, Any] | str) -> Any:
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
    context = ToolContext(context=None, tool_name=tool.name, tool_call_id="call-1", tool_arguments=raw)
    return await invoke_function_tool(function_tool=tool, context=context, arguments=raw)


def guarded(ledger: Ledger, policy: GuardPolicy, **kwargs: Any) -> tuple[dict[str, FunctionTool], AgentsToolGuard]:
    guard = create_guard(policy, receipts=False, **kwargs)
    return by_name([guard.wrap(tool) for tool in make_tools(ledger)]), guard


# -- classification ----------------------------------------------------------------------


def test_name_heuristics() -> None:
    assert classify_tool_name("get_ticket") == "read"
    assert classify_tool_name("listOpenInvoices") == "read"
    assert classify_tool_name("search-contacts") == "read"
    assert classify_tool_name("update_ticket") == "write"
    assert classify_tool_name("sync_everything") == "write"
    assert classify_tool_name("delete_ticket") == "destructive"
    assert classify_tool_name("issueRefund") == "destructive"
    assert classify_tool_name("get_and_remove_item") == "destructive"
    assert classify_tool_name("") == "write"


def test_explicit_classes_override_the_heuristic() -> None:
    ledger = Ledger()
    policy = GuardPolicy(classes={"sync_everything": "read"})
    _, guard = guarded(ledger, policy, classes={"get_ticket": "write"})
    assert guard.classify("sync_everything") == "read"
    assert guard.classify("get_ticket") == "write"


# -- decisions through the invoker --------------------------------------------------------


async def test_reads_run_and_writes_are_refused_without_rules() -> None:
    ledger = Ledger()
    tools, guard = guarded(ledger, GuardPolicy())
    assert json.loads(await invoke(tools["get_ticket"], {"ticket_id": "T-1"}))["status"] == "open"
    refused = await invoke(tools["update_ticket"], {"ticket_id": "T-1", "status": "closed"})
    assert isinstance(refused, str)
    assert refused.startswith("benchpress refused 'update_ticket' [no_allow_rule]")
    assert ledger.ran == [("get_ticket", {"ticket_id": "T-1"})]
    assert [line["decision"] for line in guard.receipts] == ["allow", "refuse"]


async def test_allow_rule_arguments_and_max_calls() -> None:
    ledger = Ledger()
    rule = GuardRule(tool="update_*", arguments={"status": "pending|resolved", "ticket_id": r"T-\d+"}, max_calls=1)
    tools, guard = guarded(ledger, GuardPolicy(rules=[rule]))
    mismatch = await invoke(tools["update_ticket"], {"ticket_id": "T-1", "status": "resolved-and-closed"})
    assert "[arguments_mismatch]" in mismatch
    assert await invoke(tools["update_ticket"], {"ticket_id": "T-1", "status": "resolved"}) == "T-1 -> resolved"
    second = await invoke(tools["update_ticket"], {"ticket_id": "T-2", "status": "pending"})
    assert "[max_calls]" in second
    assert [name for name, _ in ledger.ran] == ["update_ticket"]
    assert [line["rule"] for line in guard.receipts] == ["arguments_mismatch", "allow_rule", "max_calls"]


async def test_destructive_needs_allow_destructive_and_deny_rules_win() -> None:
    ledger = Ledger()
    tools, _ = guarded(ledger, GuardPolicy(rules=[GuardRule(tool="*")]))
    assert "[destructive_default_deny]" in await invoke(tools["delete_ticket"], {"ticket_id": "T-1"})
    assert await invoke(tools["sync_everything"], {}) == "synced"

    policy = GuardPolicy(
        rules=[
            GuardRule(tool="sync_*", effect="deny", reason="sync is off limits for agents"),
            GuardRule(tool="delete_ticket", allow_destructive=True),
        ]
    )
    tools, _ = guarded(ledger, policy)
    assert await invoke(tools["delete_ticket"], {"ticket_id": "T-1"}) == "T-1 deleted"
    denied = await invoke(tools["sync_everything"], {})
    assert denied == "benchpress refused 'sync_everything' [deny_rule]: sync is off limits for agents"
    assert [name for name, _ in ledger.ran] == ["sync_everything", "delete_ticket"]


async def test_reads_can_be_denied_and_bad_arguments_are_refused() -> None:
    ledger = Ledger()
    tools, _ = guarded(ledger, GuardPolicy(reads="deny"))
    assert "[reads_denied]" in await invoke(tools["get_ticket"], {"ticket_id": "T-1"})
    tools, guard = guarded(ledger, GuardPolicy(rules=[GuardRule(tool="update_ticket")]))
    assert "[invalid_arguments]" in await invoke(tools["update_ticket"], "{not json")
    assert "[invalid_arguments]" in await invoke(tools["update_ticket"], "[1, 2]")
    assert ledger.ran == []
    assert all(line["decision"] == "refuse" for line in guard.receipts)


async def test_direct_on_invoke_tool_is_guarded_too() -> None:
    ledger = Ledger()
    tools, _ = guarded(ledger, GuardPolicy())
    tool = tools["delete_ticket"]
    raw = json.dumps({"ticket_id": "T-9"})
    context = ToolContext(context=None, tool_name=tool.name, tool_call_id="call-2", tool_arguments=raw)
    assert "[no_allow_rule]" in await tool.on_invoke_tool(context, raw)
    assert ledger.ran == []


async def test_wrapping_copies_and_leaves_the_original_tool_unguarded() -> None:
    ledger = Ledger()
    originals = by_name(make_tools(ledger))
    wrapped = by_name(guard_tools(list(originals.values()), GuardPolicy(), receipts=False))
    assert wrapped["delete_ticket"] is not originals["delete_ticket"]
    assert wrapped["delete_ticket"].params_json_schema == originals["delete_ticket"].params_json_schema
    assert await invoke(originals["delete_ticket"], {"ticket_id": "T-3"}) == "T-3 deleted"


def test_non_function_tools_are_rejected() -> None:
    with pytest.raises(TypeError, match="FunctionTool"):
        guard_tools([object()], GuardPolicy(), receipts=False)  # pyright: ignore[reportArgumentType]


# -- body errors and receipts ------------------------------------------------------------


async def test_raising_body_is_recorded_and_propagates(tmp_path: Path) -> None:
    @function_tool(failure_error_function=None)
    def update_flaky(record_id: str) -> str:
        """Always fails."""
        raise RuntimeError("backend down")

    receipts = tmp_path / "receipts.jsonl"
    (tool,) = guard_tools([update_flaky], GuardPolicy(rules=[GuardRule(tool="update_*")]), receipts=receipts)
    with pytest.raises(Exception, match="backend down"):
        await invoke(tool, {"record_id": "R-1"})
    (line,) = [json.loads(row) for row in receipts.read_text().splitlines()]
    assert line["decision"] == "allow" and line["upstream_error"] is True


async def test_receipts_file_lines_never_store_argument_values(tmp_path: Path) -> None:
    ledger = Ledger()
    policy_file = tmp_path / "guard.json"
    policy_file.write_text(json.dumps({"rules": [{"tool": "update_ticket", "max_calls": 5}]}))
    tools = by_name(guard_tools(make_tools(ledger), policy_file))
    await invoke(tools["update_ticket"], {"ticket_id": "T-77", "status": "secret-status"})
    await invoke(tools["delete_ticket"], {"ticket_id": "T-77"})
    text = (tmp_path / DEFAULT_RECEIPTS_NAME).read_text()
    assert "secret-status" not in text and "T-77" not in text
    first, second = [json.loads(row) for row in text.splitlines()]
    expected_keys = {"ts", "tool", "class", "args_digest", "decision", "rule", "policy_rule", "reason", "latency_ms"}
    assert expected_keys <= set(first)
    assert first["class"] == "write" and first["decision"] == "allow" and first["upstream_error"] is False
    assert first["args_digest"].startswith("sha256:")
    assert second["class"] == "destructive" and second["rule"] == "no_allow_rule" and second["upstream_error"] is None


# -- policy packs ------------------------------------------------------------------------

PACK = PolicyPack.model_validate(
    {
        "name": "tickets",
        "title": "Ticket safety",
        "rules": [
            {
                "id": "tickets.no-close-without-approval",
                "reason": "closing a ticket needs an approval",
                "match": {"providers": ["tickets"], "body": "closed"},
                "unless_approved": ["close_approval"],
            }
        ],
    }
)


def ticket_call(arguments: Any) -> ProviderCall:
    return ProviderCall("tickets", "PATCH", f"/tickets/{arguments['ticket_id']}", {"status": arguments["status"]})


async def test_policy_pack_refuses_mapped_calls_and_returns_the_max_calls_slot() -> None:
    ledger = Ledger()
    policy = GuardPolicy(rules=[GuardRule(tool="update_ticket", max_calls=1)])
    tools, guard = guarded(ledger, policy, policy_packs=[PACK], actions={"update_*": ticket_call})
    refused = await invoke(tools["update_ticket"], {"ticket_id": "T-1", "status": "closed"})
    assert "[policy_pack]" in refused and "tickets.no-close-without-approval" in refused
    assert guard.receipts[-1]["pack_rule"] == "pack:tickets.no-close-without-approval"
    assert await invoke(tools["update_ticket"], {"ticket_id": "T-1", "status": "pending"}) == "T-1 -> pending"
    assert ledger.ran == [("update_ticket", {"ticket_id": "T-1", "status": "pending"})]


async def test_policy_pack_lifted_by_approval_and_failing_mapper_fails_closed() -> None:
    ledger = Ledger()
    policy = GuardPolicy(rules=[GuardRule(tool="update_ticket")])
    tools, _ = guarded(
        ledger, policy, policy_packs=[PACK], actions={"update_*": ticket_call}, approvals={"close_approval": "granted"}
    )
    assert await invoke(tools["update_ticket"], {"ticket_id": "T-1", "status": "closed"}) == "T-1 -> closed"

    def broken(arguments: Any) -> ProviderCall:
        raise KeyError("missing")

    tools, _ = guarded(Ledger(), policy, policy_packs=[PACK], actions={"update_*": broken})
    assert "pack:action_mapper_error" not in (refused := await invoke(tools["update_ticket"], {"ticket_id": "T-2"}))
    assert "[policy_pack]" in refused and "action mapper" in refused


def test_release_only_returns_slots_for_allow_rules() -> None:
    guard = PolicyGuard(GuardPolicy(rules=[GuardRule(tool="update_*", max_calls=1)]))
    allowed = guard.decide_class("update_ticket", "write", {})
    assert allowed.allowed
    assert not guard.decide_class("update_ticket", "write", {}).allowed
    guard.release(allowed)
    assert guard.decide_class("update_ticket", "write", {}).allowed


# -- end to end through Runner -----------------------------------------------------------


async def test_runner_with_scripted_model_sees_refusal_and_body_never_runs() -> None:
    ledger = Ledger()
    policy = GuardPolicy(rules=[GuardRule(tool="update_ticket")])
    tools: list[Tool] = [*guard_tools(make_tools(ledger), policy, receipts=False)]
    model = ScriptedModel(
        [
            [
                function_call("get_ticket", {"ticket_id": "T-5"}, call_id="c1"),
                function_call("delete_ticket", {"ticket_id": "T-5"}, call_id="c2"),
            ],
            [function_call("update_ticket", {"ticket_id": "T-5", "status": "pending"}, call_id="c3")],
            [assistant_message("done")],
        ]
    )
    agent = Agent(name="ops", instructions="Handle the ticket.", model=model, tools=tools)
    result = await Runner.run(agent, "Tidy ticket T-5", run_config=RunConfig(tracing_disabled=True))
    assert result.final_output == "done"
    outputs = {
        str(cast(dict[str, Any], item.raw_item)["call_id"]): str(item.output)
        for item in result.new_items
        if isinstance(item, ToolCallOutputItem)
    }
    assert outputs["c2"].startswith("benchpress refused 'delete_ticket' [no_allow_rule]")
    assert outputs["c3"] == "T-5 -> pending"
    assert [name for name, _ in ledger.ran] == ["get_ticket", "update_ticket"]


def test_import_benchpress_does_not_import_the_sdk() -> None:
    code = "import sys, benchpress; assert 'agents' not in sys.modules, 'agents imported'"
    subprocess.run([sys.executable, "-c", code], check=True)
