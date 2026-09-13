"""The stock baseline loop as a candidate inside the UNMODIFIED ArgaBench runner.

    python -m devsim run --task ECOM-02 --profile baseline-deepseek-v4-pro \
        --candidate module:devsim.baseline_candidate:baseline

Same shape as `devsim.benchpress_candidate`, wrapping `evals.baseline.invoke_baseline` (a
chat-completions port of the harness's own tool loop: harness system prompt verbatim, no addendum,
no gate, no read-back). The harness grades both arms with the same graders.
"""

from __future__ import annotations

from time import monotonic
from typing import Any

from devsim.candidates import Candidate, Provider, ToolExecutor, invocation_config, provider_for_model
from devsim.harness import harness_module
from evals.baseline import invoke_baseline


def factory(*, provider: Provider | None = None) -> Candidate:
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
        result = await invoke_baseline(
            model_id=model_id,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            tool_schema=tool_schema,
            execute_tool=execute_tool,
            max_tool_calls=max_tool_calls,
            timeout_seconds=timeout_seconds,
        )
        config = invocation_config(
            model_id=model_id,
            provider=resolved_provider,
            api_effort=api_effort,
            thinking=thinking,
            max_tool_calls=max_tool_calls,
            timeout_seconds=timeout_seconds,
            candidate="devsim.baseline_candidate",
        )
        config.update({key: value for key, value in result.config.items() if key not in config})
        events: list[dict[str, Any]] = [
            {
                "type": "invocation_started",
                "provider": resolved_provider,
                "requested_model": model_id,
                "config": config,
            },
            *result.events,
            {
                "type": "assistant_response",
                "response_model": result.response_model or model_id,
                "stop_reason": result.stop_reason,
                "content": [{"type": "text", "text": result.final_text}],
                "usage": result.usage,
            },
        ]
        models = harness_module("arga_twins_benchmark.agents.models")
        return models.ModelInvocationResult(
            requested_model=model_id,
            response_model=result.response_model or model_id,
            provider=resolved_provider,
            final_text=result.final_text,
            status=result.status,
            stop_reason=result.stop_reason,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            events=tuple(events),
            usage=result.usage,
            config=config,
            latency_ms=max(result.latency_ms, round((monotonic() - started) * 1000)),
            tool_calls=result.tool_calls,
        )

    return invoke


def baseline() -> Candidate:
    return factory()
