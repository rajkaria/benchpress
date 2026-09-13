"""Benchpress as a candidate inside the UNMODIFIED ArgaBench runner.

    python -m devsim run --task ECOM-02 --profile benchpress-deepseek-v4-pro \
        --candidate module:devsim.benchpress_candidate:benchpress

The runner calls `invoke_model(...)`; this factory returns a callable with that exact signature
that runs the Benchpress controller against the runner's own `execute_tool` (the harness's
ProviderGateway) and converts the result into the harness's `ModelInvocationResult`. Ablation
variants are separate factories so a profile can name them.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from time import monotonic
from typing import Any

from benchpress.adapter import invoke as benchpress_invoke
from benchpress.phases.common import Ablations
from devsim.candidates import Candidate, Provider, ToolExecutor, invocation_config, provider_for_model
from devsim.harness import harness_module


def factory(
    *, ablations: str | None = None, trace_root: Path | None = None, provider: Provider | None = None
) -> Candidate:
    resolved_ablations = Ablations.parse(ablations if ablations is not None else os.environ.get("BENCHPRESS_ABLATIONS"))
    root = trace_root or Path(os.environ.get("BENCHPRESS_TRACE_ROOT", "runs/devsim/receipts"))

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
        trial_id = f"bp-{uuid.uuid4().hex[:8]}"
        resolved_provider = provider or provider_for_model(model_id)
        record = await benchpress_invoke(
            model_id,
            system_prompt,
            user_prompt,
            tool_schema,
            execute_tool,
            max_tool_calls,
            timeout_seconds,
            api_effort=api_effort,
            thinking=thinking,
            ablations=resolved_ablations,
            trace_dir=root / trial_id,
            trial_id=trial_id,
        )
        config = invocation_config(
            model_id=model_id,
            provider=resolved_provider,
            api_effort=api_effort,
            thinking=thinking,
            max_tool_calls=max_tool_calls,
            timeout_seconds=timeout_seconds,
            candidate="devsim.benchpress_candidate",
        )
        config.update({key: value for key, value in record.config.items() if key not in config})
        config["trace_dir"] = str(root / trial_id)
        events: list[dict[str, Any]] = [
            {
                "type": "invocation_started",
                "provider": resolved_provider,
                "requested_model": model_id,
                "config": config,
            },
            *record.events,
            {
                "type": "assistant_response",
                "response_model": model_id,
                "stop_reason": record.stop_reason,
                "content": [{"type": "text", "text": record.final_text}],
                "usage": record.usage,
            },
        ]
        models = harness_module("arga_twins_benchmark.agents.models")
        return models.ModelInvocationResult(
            requested_model=model_id,
            response_model=model_id,
            provider=resolved_provider,
            final_text=record.final_text,
            status=record.status,
            stop_reason=record.stop_reason,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            events=tuple(events),
            usage=record.usage,
            config=config,
            latency_ms=max(record.latency_ms, round((monotonic() - started) * 1000)),
            tool_calls=record.tool_calls,
        )

    return invoke


def benchpress() -> Candidate:
    return factory()


def benchpress_no_gate() -> Candidate:
    return factory(ablations="no_gate")


def benchpress_no_policy_sweep() -> Candidate:
    return factory(ablations="no_policy_sweep")


def benchpress_no_readback() -> Candidate:
    return factory(ablations="no_readback")
