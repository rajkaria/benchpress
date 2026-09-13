"""The guard policy shared by every Benchpress shim: rules, decisions and JSONL receipts, with no SDK imports.

`benchpress mcp-guard` (MCP) and `benchpress.shims.openai_agents` (OpenAI Agents SDK) both decide tool
calls with this model, so one policy file governs both. A shim supplies the tool's class
(`read` / `write` / `destructive`) its own way; the decision itself is pure:

* rules are checked in order, matched by tool-name glob. A matching `deny` rule refuses. A matching
  `allow` rule applies when every constrained argument fully matches its regex, and counts against
  its `max_calls`; destructive tools stay refused unless that rule sets `allow_destructive`;
* with no applicable `allow` rule, reads follow `policy.reads` (allow by default) and writes are refused.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ToolClass = Literal["read", "write", "destructive"]

DecisionRule = Literal[
    "read",
    "allow_rule",
    "unknown_tool",
    "invalid_arguments",
    "deny_rule",
    "arguments_mismatch",
    "max_calls",
    "destructive_default_deny",
    "no_allow_rule",
    "reads_denied",
    "policy_pack",
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
    """A guard policy file (JSON)."""

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
    pack_rule: str | None = None


@dataclass
class PolicyGuard:
    """Pure decisions plus the receipt log. `max_calls` counters live as long as the instance."""

    policy: GuardPolicy
    receipts_path: Path | None = None
    _allowed_by_rule: dict[int, int] = field(default_factory=dict[int, int])

    def decide_class(self, name: str, tool_class: ToolClass, arguments: Mapping[str, Any]) -> Decision:
        """Decide one call of `name`, already classified `tool_class`. An allow counts against `max_calls`."""
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

    def release(self, decision: Decision) -> None:
        """Return an allow's `max_calls` slot when a later check (a policy pack) refuses the call after all."""
        index = decision.policy_rule
        if decision.rule == "allow_rule" and index is not None and self._allowed_by_rule.get(index, 0) > 0:
            self._allowed_by_rule[index] -= 1

    def record(
        self,
        name: str,
        arguments: Mapping[str, Any],
        decision: Decision,
        started: float,
        *,
        upstream_error: bool | None = None,
    ) -> dict[str, Any]:
        """Append one JSONL receipt line (argument values are never stored, only a digest) and return it."""
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
        if decision.pack_rule is not None:
            line["pack_rule"] = decision.pack_rule
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
