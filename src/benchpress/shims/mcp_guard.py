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

import fnmatch
import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

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

from benchpress.shims.mcp import McpToolRouter, ToolClass, classify_tool

DEFAULT_RECEIPTS_NAME = "mcp-guard-receipts.jsonl"

DecisionRule = Literal[
    "read",
    "allow_rule",
    "unknown_tool",
    "deny_rule",
    "arguments_mismatch",
    "max_calls",
    "destructive_default_deny",
    "no_allow_rule",
    "reads_denied",
]


class GuardRule(BaseModel):
    """One policy rule. `tool` is a glob over tool names (`create_*`, `*`)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: str = Field(min_length=1)
    effect: Literal["allow", "deny"] = "allow"
    arguments: dict[str, str] = Field(default_factory=dict[str, str])
    max_calls: int | None = Field(default=None, ge=0)
    allow_destructive: bool = False
    reason: str = ""

    @field_validator("arguments")
    @classmethod
    def _regexes_compile(cls, value: dict[str, str]) -> dict[str, str]:
        for name, pattern in value.items():
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"argument {name!r}: invalid regex {pattern!r} ({exc})") from exc
        return value

    def matches_tool(self, name: str) -> bool:
        return fnmatch.fnmatchcase(name, self.tool)

    def argument_mismatch(self, arguments: Mapping[str, Any]) -> str | None:
        """The first constrained argument that is missing or does not fully match, as a reason."""
        for name, pattern in self.arguments.items():
            if name not in arguments:
                return f"argument {name!r} is required by the rule for {self.tool!r}"
            if re.fullmatch(pattern, _render(arguments[name])) is None:
                return f"argument {name!r} does not match {pattern!r}"
        return None


class GuardPolicy(BaseModel):
    """The guard's policy file (JSON)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rules: list[GuardRule] = Field(default_factory=list[GuardRule])
    classes: dict[str, ToolClass] = Field(default_factory=dict[str, ToolClass])
    reads: Literal["allow", "deny"] = "allow"
    receipts: str | None = None

    @classmethod
    def load(cls, path: str | Path) -> GuardPolicy:
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Decision:
    allowed: bool
    tool_class: ToolClass | None
    rule: DecisionRule
    reason: str
    policy_rule: int | None = None


@dataclass
class Guard:
    """Pure decisions plus the receipt log. One instance per proxied upstream."""

    policy: GuardPolicy
    receipts_path: Path | None = None
    _allowed_by_rule: dict[int, int] = field(default_factory=dict[int, int])

    def decide(self, name: str, tool: Tool | None, arguments: Mapping[str, Any]) -> Decision:
        if tool is None:
            return Decision(False, None, "unknown_tool", f"the upstream server does not list a tool named {name!r}")
        tool_class = classify_tool(tool, self.policy.classes.get(name))
        mismatch: tuple[int, str] | None = None
        for index, rule in enumerate(self.policy.rules):
            if not rule.matches_tool(name):
                continue
            if rule.effect == "deny":
                reason = rule.reason or f"policy rule {index} ({rule.tool!r}) denies this tool"
                return Decision(False, tool_class, "deny_rule", reason, index)
            problem = rule.argument_mismatch(arguments)
            if problem is not None:
                mismatch = mismatch or (index, problem)
                continue
            if rule.max_calls is not None and self._allowed_by_rule.get(index, 0) >= rule.max_calls:
                reason = f"policy rule {index} ({rule.tool!r}) allows at most {rule.max_calls} call(s)"
                return Decision(False, tool_class, "max_calls", reason, index)
            if tool_class == "destructive" and not rule.allow_destructive:
                reason = f"{name!r} is destructive; destructive tools are refused unless a rule sets allow_destructive"
                return Decision(False, tool_class, "destructive_default_deny", reason, index)
            self._allowed_by_rule[index] = self._allowed_by_rule.get(index, 0) + 1
            return Decision(True, tool_class, "allow_rule", rule.reason or f"allowed by policy rule {index}", index)
        if tool_class == "read":
            if self.policy.reads == "allow":
                return Decision(True, tool_class, "read", "read-only tool; reads are allowed by policy")
            return Decision(False, tool_class, "reads_denied", "the policy denies reads without an allow rule")
        if mismatch is not None:
            return Decision(False, tool_class, "arguments_mismatch", mismatch[1], mismatch[0])
        reason = f"{name!r} is classified {tool_class} and no policy rule allows it"
        return Decision(False, tool_class, "no_allow_rule", reason)

    def record(
        self,
        name: str,
        arguments: Mapping[str, Any],
        decision: Decision,
        started: float,
        *,
        upstream_error: bool | None = None,
    ) -> dict[str, Any]:
        line: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "tool": name,
            "class": decision.tool_class,
            "args_digest": arguments_digest(arguments),
            "decision": "allow" if decision.allowed else "refuse",
            "rule": decision.rule,
            "policy_rule": decision.policy_rule,
            "reason": decision.reason,
            "latency_ms": round((time.monotonic() - started) * 1000, 2),
            "upstream_error": upstream_error,
        }
        if self.receipts_path is not None:
            self.receipts_path.parent.mkdir(parents=True, exist_ok=True)
            with self.receipts_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n")
        return line


def arguments_digest(arguments: Mapping[str, Any]) -> str:
    """sha256 of the canonical JSON arguments. Receipts never store argument values."""
    canonical = json.dumps(dict(arguments), sort_keys=True, ensure_ascii=False, default=str)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _render(value: object) -> str:
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


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
