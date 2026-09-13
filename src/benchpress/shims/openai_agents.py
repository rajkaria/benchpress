"""Benchpress authority in front of OpenAI Agents SDK function tools.

    from agents import Agent, function_tool
    from benchpress.shims.openai_agents import guard_tools

    tools = guard_tools([get_invoice, update_invoice, delete_invoice], "guard.json")
    agent = Agent(name="ops", instructions="...", tools=tools)

`guard_tools` returns copies of your `FunctionTool`s whose `on_invoke_tool` decides every call in code
before the tool body runs. Because the check lives in the invoker itself (not in a guardrail list the
caller could leave out), it holds for `Runner.run`, streamed runs, agents-as-tools and direct
`on_invoke_tool` calls alike. Everything else on the tool (schema, `needs_approval`, guardrails,
timeouts, failure handling) is carried over unchanged.

* **Class.** `classes` passed here, else the policy's `classes`, else `classify_tool_name`: a destructive
  verb anywhere in the name (`delete`, `refund`, `cancel`, ...) is `destructive`; a leading read verb
  (`get`, `list`, `search`, ...) is `read`; anything else is `write`. Classification is declared or
  inferred from the name, never from what the body does.
* **Decision.** The policy model shared with `benchpress mcp-guard` (`benchpress.shims.guard_policy`):
  deny rules, allow rules with full-match argument regexes and `max_calls`, destructive refused unless a
  rule sets `allow_destructive`, reads per `policy.reads`. Arguments that are not a JSON object are refused.
* **Policy packs.** For a non-read call the policy allowed, `actions` may map the tool (by name glob) to a
  `ProviderCall`; Benchpress policy packs then judge that call exactly as the mutation gate would, with
  `approvals` as the definition-of-done facts that can lift a rule. Packs only ever add refusals.
* **Refusal.** The model receives a string (the SDK's convention for a tool error the run survives):
  `benchpress refused 'delete_invoice' [destructive_default_deny]: ...`. The body never runs.
* **Receipts.** Every call appends one JSONL line: tool, class, argument digest, decision, rule, reason,
  latency, and whether the body raised. Argument values are never stored.
"""

from __future__ import annotations

import copy
import fnmatch
import json
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

try:
    from agents import FunctionTool
    from agents.tool_context import ToolContext
except ImportError as exc:  # pragma: no cover - exercised only without the extra installed
    raise ImportError(
        "benchpress.shims.openai_agents needs the OpenAI Agents SDK: install the extra with "
        "`pip install 'benchpress-agent[openai-agents]'`"
    ) from exc

from benchpress.context import Action, ActionKind, Context, DefinitionOfDone
from benchpress.packs import PolicyPack
from benchpress.shims.guard_policy import (
    Decision,
    GuardPolicy,
    GuardRule,
    PolicyGuard,
    ToolClass,
    arguments_digest,
)

__all__ = [
    "DEFAULT_RECEIPTS_NAME",
    "DESTRUCTIVE_VERBS",
    "READ_VERBS",
    "ActionMapper",
    "AgentsToolGuard",
    "Decision",
    "GuardPolicy",
    "GuardRule",
    "ProviderCall",
    "arguments_digest",
    "classify_tool_name",
    "create_guard",
    "guard_tools",
    "refusal_message",
]

DEFAULT_RECEIPTS_NAME = "openai-agents-guard-receipts.jsonl"

READ_VERBS: frozenset[str] = frozenset(
    {
        "check", "count", "describe", "download", "export", "fetch", "find", "get", "inspect", "list",
        "load", "lookup", "peek", "preview", "query", "read", "retrieve", "search", "show", "summarize", "view",
    }
)  # fmt: skip
DESTRUCTIVE_VERBS: frozenset[str] = frozenset(
    {
        "cancel", "chargeback", "delete", "destroy", "drop", "erase", "kill", "purge", "refund", "remove",
        "revoke", "terminate", "truncate", "void", "wipe",
    }
)  # fmt: skip

WriteMethod = Literal["POST", "PUT", "PATCH", "DELETE"]
_KIND_BY_METHOD: Mapping[WriteMethod, ActionKind] = {
    "POST": "create",
    "PUT": "update",
    "PATCH": "update",
    "DELETE": "update",
}


@dataclass(frozen=True)
class ProviderCall:
    """A tool call expressed as the provider write it performs, so policy packs can judge it."""

    provider: str
    method: WriteMethod
    path: str
    body: object | None = None


type ActionMapper = Callable[[Mapping[str, Any]], ProviderCall | None]


def _name_tokens(name: str) -> list[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    return [token for token in re.split(r"[^a-z0-9]+", spaced.lower()) if token]


def classify_tool_name(name: str) -> ToolClass:
    """A tool's class from its name: any destructive verb wins, then a leading read verb, else write."""
    tokens = _name_tokens(name)
    if any(token in DESTRUCTIVE_VERBS for token in tokens):
        return "destructive"
    if tokens and tokens[0] in READ_VERBS:
        return "read"
    return "write"


def refusal_message(name: str, decision: Decision) -> str:
    return f"benchpress refused {name!r} [{decision.rule}]: {decision.reason}"


@dataclass
class AgentsToolGuard:
    """One policy, its counters and its receipts, shared by every tool it wraps."""

    guard: PolicyGuard
    classes: Mapping[str, ToolClass] = field(default_factory=dict[str, ToolClass])
    policy_packs: tuple[PolicyPack, ...] = ()
    actions: Mapping[str, ActionMapper] = field(default_factory=dict[str, ActionMapper])
    approvals: Mapping[str, str] = field(default_factory=dict[str, str])
    receipts: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    def classify(self, name: str) -> ToolClass:
        return self.classes.get(name) or self.guard.policy.classes.get(name) or classify_tool_name(name)

    def decide(self, name: str, raw_arguments: str) -> tuple[Decision, dict[str, Any]]:
        """Decide one call from the model's raw JSON arguments. Returns the decision and the parsed arguments."""
        tool_class = self.classify(name)
        try:
            parsed: object = json.loads(raw_arguments) if raw_arguments.strip() else {}
        except json.JSONDecodeError as exc:
            return Decision(False, tool_class, "invalid_arguments", f"arguments are not valid JSON ({exc.msg})"), {}
        if not isinstance(parsed, dict):
            return Decision(False, tool_class, "invalid_arguments", "arguments must be a JSON object"), {}
        arguments = cast(dict[str, Any], parsed)
        decision = self.guard.decide_class(name, tool_class, arguments)
        if not decision.allowed or tool_class == "read" or not self.policy_packs:
            return decision, arguments
        refusal = self._pack_refusal(name, tool_class, arguments)
        if refusal is None:
            return decision, arguments
        self.guard.release(decision)
        pack_rule, reason = refusal
        return Decision(False, tool_class, "policy_pack", reason, decision.policy_rule, pack_rule), arguments

    def _pack_refusal(self, name: str, tool_class: ToolClass, arguments: Mapping[str, Any]) -> tuple[str, str] | None:
        mapper = next((mapper for glob, mapper in self.actions.items() if fnmatch.fnmatchcase(name, glob)), None)
        if mapper is None:
            return None
        try:
            call = mapper(arguments)
        except Exception as exc:  # noqa: BLE001 - a mapper that cannot express the call fails closed
            return ("pack:action_mapper_error", f"the action mapper for {name!r} raised {type(exc).__name__}: {exc}")
        if call is None:
            return None
        action = Action(
            id=name,
            kind=_KIND_BY_METHOD[call.method],
            provider=call.provider,
            method=call.method,
            path=call.path,
            body=call.body,
            rationale=f"{tool_class} tool call",
        )
        context = Context(dod=DefinitionOfDone(facts=dict(self.approvals)))
        for pack in self.policy_packs:
            refusal = pack.refusal(action, context)
            if refusal is not None:
                return refusal
        return None

    def record(
        self, name: str, arguments: Mapping[str, Any], decision: Decision, started: float, *, body_error: bool | None
    ) -> dict[str, Any]:
        line = self.guard.record(name, arguments, decision, started, upstream_error=body_error)
        self.receipts.append(line)
        return line

    def wrap(self, tool: FunctionTool) -> FunctionTool:
        """A copy of `tool` whose invoker enforces this guard before the original body runs."""
        if not isinstance(tool, FunctionTool):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError(
                f"guard_tools wraps FunctionTool only, got {type(tool).__name__}; hosted and computer tools "
                "run outside the Python process and cannot be guarded here"
            )
        guarded = copy.copy(tool)
        inner: Callable[[ToolContext[Any], str], Awaitable[Any]] = guarded.on_invoke_tool
        name = tool.name

        async def on_invoke_tool(ctx: ToolContext[Any], input: str) -> Any:
            started = time.monotonic()
            decision, arguments = self.decide(name, input)
            if not decision.allowed:
                self.record(name, arguments, decision, started, body_error=None)
                return refusal_message(name, decision)
            try:
                result = await inner(ctx, input)
            except Exception:
                self.record(name, arguments, decision, started, body_error=True)
                raise
            self.record(name, arguments, decision, started, body_error=False)
            return result

        guarded.on_invoke_tool = on_invoke_tool
        return guarded


def _policy_and_base(policy: GuardPolicy | Mapping[str, Any] | str | Path) -> tuple[GuardPolicy, Path]:
    if isinstance(policy, GuardPolicy):
        return policy, Path.cwd()
    if isinstance(policy, str | Path):
        return GuardPolicy.load(policy), Path(policy).resolve().parent
    return GuardPolicy.model_validate(dict(policy)), Path.cwd()


def create_guard(
    policy: GuardPolicy | Mapping[str, Any] | str | Path,
    *,
    receipts: str | Path | Literal[False] | None = None,
    classes: Mapping[str, ToolClass] | None = None,
    policy_packs: Sequence[PolicyPack] = (),
    actions: Mapping[str, ActionMapper] | None = None,
    approvals: Mapping[str, str] | None = None,
) -> AgentsToolGuard:
    """A guard to wrap tools with (`guard.wrap(tool)`); keep it to read `guard.receipts` in memory.

    `policy` is a `GuardPolicy`, its dict form, or a path to the JSON file. `receipts` is the JSONL path;
    by default the policy's `receipts` (relative to the policy file when loaded from a path, else the
    working directory), else `openai-agents-guard-receipts.jsonl` there. `False` writes no file (the
    lines stay in `guard.receipts`). `classes` overrides the policy's classes and the name heuristic.
    """
    loaded, base = _policy_and_base(policy)
    if receipts is False:
        receipts_path = None
    elif receipts is not None:
        receipts_path = Path(receipts)
    else:
        receipts_path = base / (loaded.receipts or DEFAULT_RECEIPTS_NAME)
    return AgentsToolGuard(
        guard=PolicyGuard(loaded, receipts_path=receipts_path),
        classes=dict(classes or {}),
        policy_packs=tuple(policy_packs),
        actions=dict(actions or {}),
        approvals=dict(approvals or {}),
    )


def guard_tools(
    tools: Sequence[FunctionTool],
    policy: GuardPolicy | Mapping[str, Any] | str | Path,
    *,
    receipts: str | Path | Literal[False] | None = None,
    classes: Mapping[str, ToolClass] | None = None,
    policy_packs: Sequence[PolicyPack] = (),
    actions: Mapping[str, ActionMapper] | None = None,
    approvals: Mapping[str, str] | None = None,
) -> list[FunctionTool]:
    """Guarded copies of `tools`, sharing one policy, one set of `max_calls` counters and one receipt log.

    Arguments as in `create_guard`. Build the guard yourself when several agents must share counters.
    """
    guard = create_guard(
        policy, receipts=receipts, classes=classes, policy_packs=policy_packs, actions=actions, approvals=approvals
    )
    return [guard.wrap(tool) for tool in tools]
