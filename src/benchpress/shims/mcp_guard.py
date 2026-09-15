"""`benchpress mcp-guard`: an MCP proxy that refuses writes in code unless a policy allows them.

    benchpress mcp-guard --policy guard.json -- <upstream command...>
    benchpress mcp-guard --policy guard.json --http 127.0.0.1:8788 -- <upstream command...>

The guard launches the upstream MCP server as a subprocess, re-exposes its tools unchanged over stdio (or, with
`--http`, over streamable HTTP at `/mcp`), and classifies every `tools/call` before forwarding it:

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

The HTTP mode has no auth of its own, so `main` refuses a non-loopback host unless `--allow-remote` is passed.
"""

from __future__ import annotations

import ipaddress
import os
import sys
import time
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from pydantic import ValidationError

try:
    import anyio
    from mcp import Client, ClientSession, StdioServerParameters, stdio_server
    from mcp.server import Server
    from mcp.server.streamable_http_manager import StreamableHTTPASGIApp, StreamableHTTPSessionManager
    from mcp.server.transport_security import TransportSecuritySettings
    from mcp.types import (
        CallToolRequestParams,
        CallToolResult,
        ListToolsResult,
        PaginatedRequestParams,
        TextContent,
        Tool,
    )
    from starlette.applications import Starlette
    from starlette.routing import Route
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
    "guard_http_app",
    "main",
    "serve_guard",
    "serve_guard_http",
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


def _is_loopback(host: str) -> bool:
    """True for `localhost`, `127.0.0.0/8` and `::1`."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _transport_security(host: str) -> TransportSecuritySettings | None:
    """The SDK's own defaults for a loopback bind: DNS-rebinding protection, so the Host header must name a
    loopback address (`127.0.0.1:*`, `localhost:*`, `[::1]:*`, or the bound host) and a browser Origin must too.
    None on any other host, where the SDK checks neither."""
    if not _is_loopback(host):
        return None
    bound = f"[{host}]" if ":" in host else host
    names = dict.fromkeys([bound, "127.0.0.1", "localhost", "[::1]"])
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[f"{name}:*" for name in names],
        allowed_origins=[f"http://{name}:*" for name in names],
    )


def guard_http_app(upstream: ClientSession, guard: Guard, *, host: str) -> Starlette:
    """The guard over streamable HTTP: a Starlette app serving `/mcp` from one session manager (Ruling R18).

    `build_guard_server` is a lowlevel server, so the app routes `/mcp` to a `StreamableHTTPSessionManager` for it
    and runs the manager in its lifespan. `host` is the address the app will be bound to (see `_transport_security`).
    """
    manager = StreamableHTTPSessionManager(
        app=build_guard_server(upstream, guard), security_settings=_transport_security(host)
    )

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncGenerator[None]:
        async with manager.run():
            yield

    return Starlette(routes=[Route("/mcp", endpoint=StreamableHTTPASGIApp(manager))], lifespan=lifespan)


def _upstream_parameters(command: Sequence[str]) -> StdioServerParameters:
    # The guard is transparent: the upstream sees the environment the guard was started with.
    return StdioServerParameters(command=command[0], args=list(command[1:]), env=dict(os.environ))


async def serve_guard(policy: GuardPolicy, command: Sequence[str], *, receipts: Path | None) -> None:
    """Launch the upstream over stdio and serve the guard on this process's stdin/stdout."""
    async with Client(_upstream_parameters(command), mode="legacy") as upstream:
        server = build_guard_server(upstream.session, Guard(policy, receipts_path=receipts))
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())


async def serve_guard_http(
    policy: GuardPolicy, command: Sequence[str], *, receipts: Path | None, host: str, port: int
) -> None:
    """Launch the upstream over stdio and serve the guard over streamable HTTP at `http://host:port/mcp`."""
    import uvicorn  # only the HTTP mode needs a web server, so the stdio guard never loads one

    async with Client(_upstream_parameters(command), mode="legacy") as upstream:
        app = guard_http_app(upstream.session, Guard(policy, receipts_path=receipts), host=host)
        await uvicorn.Server(uvicorn.Config(app, host=host, port=port)).serve()


def _parse_bind(spec: str) -> tuple[str, int] | None:
    """`HOST:PORT` (an IPv6 host may be bracketed) as a host and a port from 1 to 65535, or None."""
    host, separator, port = spec.rpartition(":")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if not separator or not host or not (port.isascii() and port.isdigit()) or not 1 <= int(port) <= 65535:
        return None
    return host, int(port)


def main(
    policy_path: str,
    upstream: Sequence[str],
    receipts: str | None = None,
    *,
    http: str | None = None,
    allow_remote: bool = False,
) -> int:
    """Entry point for `benchpress mcp-guard`. Over stdio nothing but protocol may reach stdout.

    With `http` (`HOST:PORT`) the guard serves streamable HTTP instead. It has no auth, so a non-loopback host
    exits 2 unless `allow_remote`, which serves with a warning on stderr.
    """
    command = list(upstream)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("mcp-guard: an upstream command is required after `--`", file=sys.stderr)
        return 2
    bind = _parse_bind(http) if http is not None else None
    if http is not None:
        if bind is None:
            print(f"mcp-guard: --http expects HOST:PORT with a port from 1 to 65535, got {http!r}", file=sys.stderr)
            return 2
        if not _is_loopback(bind[0]) and not allow_remote:
            print("mcp-guard: the guard has no auth; bind to loopback or pass --allow-remote", file=sys.stderr)
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
    resolved = receipts_path.resolve()
    if bind is None:
        anyio.run(lambda: serve_guard(policy, command, receipts=resolved))
        return 0
    host, port = bind
    if not _is_loopback(host):
        print(
            f"mcp-guard: warning: serving on {host}:{port} with no auth; anyone who can reach it can call every "
            "tool the policy allows",
            file=sys.stderr,
        )
    anyio.run(lambda: serve_guard_http(policy, command, receipts=resolved, host=host, port=port))
    return 0
