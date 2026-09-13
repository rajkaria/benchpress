"""`benchpress mcp-guard`: an MCP stdio proxy that refuses writes in code unless a policy allows them.

    benchpress mcp-guard --policy guard.json -- <upstream command...>

The guard launches the upstream MCP server as a subprocess, re-exposes its tools unchanged, and
classifies every `tools/call` before forwarding it:

* the tool's class comes from `policy.classes`, else its annotations, else the MCP default
  (`destructive`), exactly as in `benchpress.shims.mcp.classify_tool`;
* rules are checked in order, matched by tool-name glob. A matching `deny` rule refuses. A matching
  `allow` rule applies when every constrained argument fully matches its regex, and counts against
  its `max_calls`; destructive tools stay refused unless that rule sets `allow_destructive`;
* with no applicable `allow` rule, reads follow `policy.reads` (allow by default) and writes are refused.

A refusal is a normal MCP tool result with `isError: true` and the reason, so the calling model sees
why. Every call, allowed or refused, appends one JSONL receipt line (tool, class, arguments digest,
decision, rule, reason, latency). The guard does not read back what an allowed write changed; that
is the Benchpress loop's job (`benchpress.shims.mcp` + `run_trial`), not a transparent proxy's.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError

try:
    import anyio
    from mcp import Client, ClientSession, StdioServerParameters, stdio_server
    from mcp.server import Server
    from mcp.types import (
        CallToolRequestParams,
        CallToolResult,
        ListToolsResult,
        PaginatedRequestParams,
        TextContent,
        Tool,
    )
except ImportError as exc:  # pragma: no cover - exercised only without the extra installed
    raise ImportError(
        "benchpress mcp-guard needs the MCP SDK: install the extra with `pip install 'benchpress-agent[mcp]'`"
    ) from exc

from benchpress.shims.guard_policy import (
    Decision,
    DecisionRule,
    GuardPolicy,
    GuardRule,
    PolicyGuard,
    arguments_digest,
)
from benchpress.shims.mcp import McpToolRouter, classify_tool

__all__ = [
    "DEFAULT_RECEIPTS_NAME",
    "Decision",
    "DecisionRule",
    "Guard",
    "GuardPolicy",
    "GuardRule",
    "arguments_digest",
    "build_guard_server",
    "main",
    "serve_guard",
]

DEFAULT_RECEIPTS_NAME = "mcp-guard-receipts.jsonl"


class Guard(PolicyGuard):
    """The shared policy decisions (`benchpress.shims.guard_policy`) for MCP tools. One per proxied upstream."""

    def decide(self, name: str, tool: Tool | None, arguments: Mapping[str, Any]) -> Decision:
        if tool is None:
            return Decision(False, None, "unknown_tool", f"the upstream server does not list a tool named {name!r}")
        return self.decide_class(name, classify_tool(tool, self.policy.classes.get(name)), arguments)


def _error_result(text: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], is_error=True)


def build_guard_server(upstream: ClientSession, guard: Guard, *, name: str = "benchpress-mcp-guard") -> Server[Any]:
    """A lowlevel MCP server that lists the upstream's tools and forwards only calls the guard allows."""
    router = McpToolRouter(sessions={"upstream": upstream})

    async def on_list_tools(ctx: Any, params: PaginatedRequestParams | None) -> ListToolsResult:
        tools = await router.tools("upstream", refresh=True)
        return ListToolsResult(tools=list(tools.values()))

    async def on_call_tool(ctx: Any, params: CallToolRequestParams) -> CallToolResult:
        started = time.monotonic()
        arguments: dict[str, Any] = dict(params.arguments or {})
        tools = await router.tools("upstream")
        if params.name not in tools:
            tools = await router.tools("upstream", refresh=True)
        decision = guard.decide(params.name, tools.get(params.name), arguments)
        if not decision.allowed:
            guard.record(params.name, arguments, decision, started)
            return _error_result(f"benchpress mcp-guard refused {params.name!r} [{decision.rule}]: {decision.reason}")
        try:
            result = await upstream.call_tool(params.name, arguments)
        except Exception as exc:  # noqa: BLE001 - an upstream failure is reported, never crashes the proxy
            guard.record(params.name, arguments, decision, started, upstream_error=True)
            return _error_result(f"benchpress mcp-guard: upstream call failed ({type(exc).__name__}: {exc})")
        guard.record(params.name, arguments, decision, started, upstream_error=result.is_error)
        return result

    return Server(name, on_list_tools=on_list_tools, on_call_tool=on_call_tool)


async def serve_guard(policy: GuardPolicy, command: Sequence[str], *, receipts: Path | None) -> None:
    """Launch the upstream over stdio and serve the guard on this process's stdin/stdout."""
    # The guard is transparent: the upstream sees the environment the guard was started with.
    params = StdioServerParameters(command=command[0], args=list(command[1:]), env=dict(os.environ))
    async with Client(params, mode="legacy") as upstream:
        server = build_guard_server(upstream.session, Guard(policy, receipts_path=receipts))
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())


def main(policy_path: str, upstream: Sequence[str], receipts: str | None = None) -> int:
    """Entry point for `benchpress mcp-guard`. Nothing but protocol may reach stdout."""
    command = list(upstream)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("mcp-guard: an upstream command is required after `--`", file=sys.stderr)
        return 2
    try:
        policy = GuardPolicy.load(policy_path)
    except (OSError, ValidationError, ValueError) as exc:
        print(f"mcp-guard: cannot load policy {policy_path!r}: {exc}", file=sys.stderr)
        return 2
    if receipts:
        receipts_path = Path(receipts)
    elif policy.receipts:
        receipts_path = Path(policy_path).parent / policy.receipts
    else:
        receipts_path = Path(policy_path).parent / DEFAULT_RECEIPTS_NAME
    anyio.run(lambda: serve_guard(policy, command, receipts=receipts_path.resolve()))
    return 0
