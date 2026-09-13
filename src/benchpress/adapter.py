"""The ArgaBench candidate contract: `invoke(model_id, system_prompt, user_prompt, tool_schema,
execute_tool, max_tool_calls, timeout_seconds, *, api_effort, thinking)`.

`src/benchpress` never imports the harness, so this returns an `InvocationRecord` with the
same fields as the harness's `ModelInvocationResult`; the harness-side shim converts it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, cast

from benchpress.controller import run_trial
from benchpress.model import ModelConfig
from benchpress.phases.common import Ablations
from benchpress.tools import ToolExecutor

KNOWN_ROLES: frozenset[str] = frozenset(
    {
        "code_host",
        "email",
        "calendar",
        "file_storage",
        "hubspot_crm",
        "jira_tracker",
        "linear_tracker",
        "professional_network",
        "knowledge_base",
        "salesforce_crm",
        "team_chat",
        "payments",
    }
)


@dataclass(frozen=True)
class InvocationRecord:
    requested_model: str
    response_model: str | None
    provider: Literal["anthropic", "openai", "google"]
    final_text: str
    status: str
    stop_reason: str
    system_prompt: str
    user_prompt: str
    events: tuple[dict[str, Any], ...] = ()
    usage: dict[str, Any] = field(default_factory=lambda: dict[str, Any]())
    config: dict[str, Any] = field(default_factory=lambda: dict[str, Any]())
    latency_ms: int = 0
    tool_calls: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def providers_from_tool_schema(tool_schema: object) -> list[str]:
    """Provisioned provider names from the `provider_api` tool's enum (roles excluded)."""
    schemas: Sequence[Mapping[str, Any]]
    if isinstance(tool_schema, Mapping):
        schemas = [cast(Mapping[str, Any], tool_schema)]
    else:
        schemas = cast(Sequence[Mapping[str, Any]], tool_schema)
    for schema in schemas:
        if schema.get("name") != "provider_api":
            continue
        parameters = cast(Mapping[str, Any], schema.get("input_schema", schema.get("parameters", {})))
        properties = cast(Mapping[str, Any], parameters.get("properties", {}))
        provider = cast(Mapping[str, Any], properties.get("provider", {}))
        enum = cast(Sequence[object], provider.get("enum", []))
        return [str(item) for item in enum if str(item) not in KNOWN_ROLES]
    return []


async def invoke(
    model_id: str,
    system_prompt: str,
    user_prompt: str,
    tool_schema: object,
    execute_tool: ToolExecutor,
    max_tool_calls: int,
    timeout_seconds: float,
    *,
    api_effort: str = "default",
    thinking: str = "model_default",
    ablations: Ablations | None = None,
    trace_dir: Path | None = None,
    trial_id: str | None = None,
) -> InvocationRecord:
    inner_model = model_id.split("/", 1)[1] if model_id.startswith("benchpress/") else model_id
    config = ModelConfig.from_env(model=inner_model)
    if api_effort and api_effort != "default":
        config = replace(config, effort=api_effort)
    resolved_ablations = ablations or Ablations.parse(os.environ.get("BENCHPRESS_ABLATIONS"))
    result = await run_trial(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        providers=providers_from_tool_schema(tool_schema),
        execute_tool=execute_tool,
        config=config,
        ablations=resolved_ablations,
        trace_dir=trace_dir,
        trial_id=trial_id,
    )
    if result.error == "timeout":
        status, stop_reason = "timed_out", "timeout"
    elif result.error:
        status, stop_reason = "incomplete", result.error[:80]
    else:
        status, stop_reason = "completed", "end_turn"
    provider: Literal["anthropic", "openai", "google"] = "anthropic" if inner_model.startswith("claude") else "openai"
    return InvocationRecord(
        requested_model=model_id,
        response_model=model_id,
        final_text=result.final_text,
        provider=provider,
        status=status,
        stop_reason=stop_reason,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        events=result.tool_events,
        usage=result.usage.harness_usage(),
        config={
            "model": inner_model,
            "provider": config.provider,
            "effort": config.effort,
            "thinking": {"type": thinking},
            "scaffold": "benchpress",
            "ablations": list(result.ablations),
            "max_tool_calls": max_tool_calls,
            "timeout_seconds": timeout_seconds,
            "benchpress_status": result.status,
        },
        latency_ms=result.latency_ms,
        tool_calls=result.provider_calls + result.docs_calls,
    )
