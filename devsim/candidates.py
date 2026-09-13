"""Candidate factories: `invoke_model`-compatible callables the patched runner calls.

The harness calls (`scripts/run_argabench_40.py:901`):

    invoke_model(model_id=..., system_prompt=..., user_prompt=..., tool_schema=[provider_api, provider_docs],
                 execute_tool=..., max_tool_calls=200, timeout_seconds=1800, api_effort=..., thinking=...)

and expects an `arga_twins_benchmark.agents.models.ModelInvocationResult`. Events follow the stock
adapters (`agents/anthropic.py:479–490`, `agents/openai.py`): one `tool_call` event per executed
call, carrying the executor's dict **verbatim** as `output`, so `output.trace.sequence` and
`output.trace.request_fingerprint` pair with `provider-trace.json`
(`reporting/argabench_mkt_ecom_legacy.py:485–560`). Requirement text is
`normal_text({arguments, response: output.body})`, so bodies must survive untouched.

`config` satisfies every classifier effort check (`argabench_matrix.py:_invocation_effort_issues`):
`model`, `provider`, `effort` (Anthropic), `reasoning.effort` (OpenAI), `thinking.type` (Anthropic)
or the plain `thinking` string (Google).
"""

from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any, Literal, cast

from devsim.harness import harness_module

type Provider = Literal["anthropic", "openai", "google"]
type ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[object]]
type Candidate = Callable[..., Awaitable[Any]]

PROVIDER_API = "provider_api"
PROVIDER_DOCS = "provider_docs"
TOOL_USE_ID_PREFIX = "bp-"


@dataclass(frozen=True)
class ScriptedCall:
    tool: str
    input: dict[str, Any]

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> ScriptedCall:
        tool = raw.get("tool")
        payload = raw.get("input")
        if tool not in {PROVIDER_API, PROVIDER_DOCS}:
            raise ValueError(f"scripted call tool must be {PROVIDER_API!r} or {PROVIDER_DOCS!r}, got {tool!r}")
        if not isinstance(payload, Mapping):
            raise ValueError(f"scripted call input must be an object (call to {tool})")
        return cls(tool=cast(str, tool), input=dict(cast(Mapping[str, Any], payload)))


def provider_for_model(model_id: str) -> Provider:
    lowered = model_id.casefold()
    if lowered.startswith("claude"):
        return "anthropic"
    if lowered.startswith("gemini"):
        return "google"
    return "openai"


def zero_usage() -> dict[str, Any]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0},
    }


def invocation_config(
    *,
    model_id: str,
    provider: Provider,
    api_effort: str,
    thinking: str,
    max_tool_calls: int,
    timeout_seconds: float,
    candidate: str,
) -> dict[str, Any]:
    return {
        "model": model_id,
        "provider": provider,
        "endpoint": "devsim",
        "candidate": candidate,
        "effort": api_effort,
        "reasoning": {"effort": api_effort},
        "thinking": thinking if provider == "google" else {"type": thinking},
        "max_output_tokens": None,
        "temperature": None,
        "max_tool_calls": max_tool_calls,
        "timeout_seconds": timeout_seconds,
        "fallback": None,
    }


def tool_names(tool_schema: object) -> set[str]:
    schemas: list[object]
    if isinstance(tool_schema, Mapping):
        schemas = [cast(object, tool_schema)]
    elif isinstance(tool_schema, Sequence) and not isinstance(tool_schema, str | bytes):
        schemas = list(cast(Sequence[object], tool_schema))
    else:
        return set()
    names: set[str] = set()
    for schema in schemas:
        if isinstance(schema, Mapping):
            name = cast(Mapping[str, object], schema).get("name")
            if isinstance(name, str) and name:
                names.add(name)
    return names


def _result(**fields: Any) -> Any:
    models = harness_module("arga_twins_benchmark.agents.models")
    return models.ModelInvocationResult(**fields)


def _retryable_infrastructure_error() -> type[BaseException]:
    return cast(type[BaseException], harness_module("arga_twins_benchmark.errors").RetryableInfrastructureError)


def stub(*, final_text: str = "{}", provider: Provider | None = None) -> Candidate:
    """A candidate that makes no tool calls and completes with `final_text`."""

    async def invoke(
        model_id: str,
        system_prompt: str,
        user_prompt: str,
        tool_schema: object,
        execute_tool: ToolExecutor,
        max_tool_calls: int,
        timeout_seconds: float,
        *,
        api_effort: str = "high",
        thinking: str = "adaptive",
    ) -> Any:
        started = monotonic()
        resolved_provider = provider or provider_for_model(model_id)
        config = invocation_config(
            model_id=model_id,
            provider=resolved_provider,
            api_effort=api_effort,
            thinking=thinking,
            max_tool_calls=max_tool_calls,
            timeout_seconds=timeout_seconds,
            candidate="devsim.candidates.stub",
        )
        usage = zero_usage()
        events: list[dict[str, Any]] = [
            {
                "type": "invocation_started",
                "provider": resolved_provider,
                "requested_model": model_id,
                "config": config,
            },
            {
                "type": "assistant_response",
                "response_model": model_id,
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": final_text}],
                "usage": usage,
            },
        ]
        return _result(
            requested_model=model_id,
            response_model=model_id,
            provider=resolved_provider,
            final_text=final_text,
            status="completed",
            stop_reason="end_turn",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            events=tuple(events),
            usage=usage,
            config=config,
            latency_ms=round((monotonic() - started) * 1000),
            tool_calls=0,
        )

    return invoke


def scripted(
    calls: Sequence[ScriptedCall | Mapping[str, Any]],
    final_text: str,
    *,
    provider: Provider | None = None,
) -> Candidate:
    """A candidate that replays `calls` through the harness executor, then answers `final_text`."""

    script = [call if isinstance(call, ScriptedCall) else ScriptedCall.from_mapping(call) for call in calls]

    async def invoke(
        model_id: str,
        system_prompt: str,
        user_prompt: str,
        tool_schema: object,
        execute_tool: ToolExecutor,
        max_tool_calls: int,
        timeout_seconds: float,
        *,
        api_effort: str = "high",
        thinking: str = "adaptive",
    ) -> Any:
        started = monotonic()
        resolved_provider = provider or provider_for_model(model_id)
        config = invocation_config(
            model_id=model_id,
            provider=resolved_provider,
            api_effort=api_effort,
            thinking=thinking,
            max_tool_calls=max_tool_calls,
            timeout_seconds=timeout_seconds,
            candidate="devsim.candidates.scripted",
        )
        usage = zero_usage()
        names = tool_names(tool_schema)
        events: list[dict[str, Any]] = [
            {"type": "invocation_started", "provider": resolved_provider, "requested_model": model_id, "config": config}
        ]
        retryable = _retryable_infrastructure_error()
        tool_calls = 0

        def finish(status: str, stop_reason: str, text: str) -> Any:
            return _result(
                requested_model=model_id,
                response_model=model_id,
                provider=resolved_provider,
                final_text=text,
                status=status,
                stop_reason=stop_reason,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                events=tuple(events),
                usage=usage,
                config=config,
                latency_ms=round((monotonic() - started) * 1000),
                tool_calls=tool_calls,
            )

        for call in script:
            if tool_calls + 1 > max_tool_calls:
                events.append(
                    {
                        "type": "tool_limit_exceeded",
                        "attempted_calls": 1,
                        "completed_calls": tool_calls,
                        "max_tool_calls": max_tool_calls,
                    }
                )
                return finish("tool_limit_exceeded", "tool_limit_exceeded", "")
            call_started = monotonic()
            arguments = dict(call.input)
            is_error = call.tool not in names
            output: object
            if is_error:
                output = {"error": {"type": "InvalidToolCall"}}
            else:
                try:
                    output = await execute_tool(call.tool, arguments)
                except asyncio.CancelledError:
                    raise
                except retryable:
                    raise
                except Exception as error:  # mirrors agents/anthropic.py:470-477
                    output = {"error": {"type": "ToolExecutionError", "exception_type": type(error).__name__}}
                    is_error = True
            tool_calls += 1
            events.append(
                {
                    "type": "tool_call",
                    "provider_call_index": tool_calls,
                    "tool_use_id": f"{TOOL_USE_ID_PREFIX}{tool_calls}",
                    "name": call.tool,
                    "arguments": arguments,
                    "output": output,
                    "is_error": is_error,
                    "latency_ms": round((monotonic() - call_started) * 1000),
                }
            )
        events.append(
            {
                "type": "assistant_response",
                "response_model": model_id,
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": final_text}],
                "usage": usage,
            }
        )
        return finish("completed", "end_turn", final_text)

    return invoke


def load_scripted_file(path: Path) -> tuple[list[ScriptedCall], str]:
    """Read `{"calls": [{"tool", "input"}, ...], "final_text": "..."}`."""

    raw: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError(f"{path}: scripted candidate file must be a JSON object")
    payload = cast(Mapping[str, Any], raw)
    raw_calls = payload.get("calls")
    if not isinstance(raw_calls, list):
        raise ValueError(f"{path}: 'calls' must be an array")
    calls = [
        ScriptedCall.from_mapping(cast(Mapping[str, Any], item))
        for item in cast(list[object], raw_calls)
        if isinstance(item, Mapping)
    ]
    if len(calls) != len(cast(list[object], raw_calls)):
        raise ValueError(f"{path}: every call must be an object")
    final_text = payload.get("final_text", "{}")
    if not isinstance(final_text, str):
        final_text = json.dumps(final_text, sort_keys=True)
    return calls, final_text


def load_candidate(spec: str, *, provider: Provider | None = None) -> Candidate:
    """Resolve a CLI spec: `stub`, `scripted:<json file>` or `module:<dotted.path>:<factory>`."""

    if spec == "stub":
        return stub(provider=provider)
    if spec.startswith("scripted:"):
        calls, final_text = load_scripted_file(Path(spec.removeprefix("scripted:")).expanduser())
        return scripted(calls, final_text, provider=provider)
    if spec.startswith("module:"):
        target = spec.removeprefix("module:")
        module_name, _, factory_name = target.rpartition(":")
        if not module_name or not factory_name:
            raise ValueError("module candidate must look like module:<dotted.path>:<factory>")
        factory = getattr(importlib.import_module(module_name), factory_name)
        candidate = cast(object, factory())
        if not callable(candidate):
            raise TypeError(f"{target} did not return a callable candidate")
        return cast(Candidate, candidate)
    raise ValueError(f"unknown candidate spec {spec!r}; use stub, scripted:<file> or module:<path>:<factory>")


__all__ = [
    "PROVIDER_API",
    "PROVIDER_DOCS",
    "TOOL_USE_ID_PREFIX",
    "Candidate",
    "Provider",
    "ScriptedCall",
    "ToolExecutor",
    "invocation_config",
    "load_candidate",
    "load_scripted_file",
    "provider_for_model",
    "scripted",
    "stub",
    "tool_names",
    "zero_usage",
]
