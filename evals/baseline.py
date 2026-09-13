"""The stock-loop baseline: one model, the harness prompts, the two harness tools, no scaffold.

This is the control arm of the Plan B comparison. It is deliberately *not* Benchpress: no gate, no
phases, no read-backs, no receipts — just `system_prompt + user_prompt + tools` in a bounded tool
loop, exactly the shape ArgaBench's own stock adapter runs.

**Disclosure.** ArgaBench's stock Anthropic adapter drives the Anthropic Messages API directly. This
is a chat-completions port of that loop (`benchpress.model.ModelClient.explore`), so that the
baseline and Benchpress share one transport and one cost meter and the only difference between the
two arms is the scaffold. Everything the harness controls is held identical: the system prompt is
passed verbatim with no addendum, the user prompt is the suite prompt verbatim, the tool schema is
the gateway's own `[provider_api, provider_docs]`, and the call / time limits are the harness's.
Per-tool-call events are recorded in the harness's own `tool_call` event shape with
`tool_use_id = "base-<n>"` and the executor's output dict stored verbatim, so the same assertions
can read either arm's trace.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, cast

from benchpress.model import ModelAPIError, ModelClient, ModelConfig, ModelRefusal
from benchpress.tools import ToolExecutor

BaselineStatus = Literal["completed", "timed_out", "tool_limit_exceeded", "api_error", "refused"]

SCAFFOLD = "stock-loop"
DISCLOSURE = (
    "chat-completions port of ArgaBench's stock single-loop adapter: harness system prompt verbatim, "
    "no addendum, same tool schema and limits, no Benchpress scaffold"
)


@dataclass(frozen=True)
class BaselineResult:
    """The same field set as the harness's `ModelInvocationResult` (returned by `agents/runner.py::invoke_model`)."""

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


class _Recorder:
    """Wraps the trial's `execute_tool` and records one harness-shaped event per call."""

    def __init__(self, execute_tool: ToolExecutor, max_tool_calls: int) -> None:
        self._execute_tool = execute_tool
        self._max_tool_calls = max_tool_calls
        self.events: list[dict[str, Any]] = []

    @property
    def calls(self) -> int:
        return len(self.events)

    async def __call__(self, tool_name: str, tool_input: dict[str, Any]) -> object:
        started = time.monotonic()
        output: object = None
        is_error = False
        try:
            output = await self._execute_tool(tool_name, tool_input)
        except Exception as exc:  # noqa: BLE001 - the model must see failures as data, like the harness
            output = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:500]}
            is_error = True
        else:
            is_error = _is_failure(output)
        index = len(self.events) + 1
        self.events.append(
            {
                "type": "tool_call",
                "provider_call_index": index,
                "tool_use_id": f"base-{index}",
                "name": tool_name,
                "arguments": dict(tool_input),
                "output": output,
                "is_error": is_error,
                "latency_ms": round((time.monotonic() - started) * 1000),
            }
        )
        return output


async def invoke_baseline(
    *,
    model_id: str,
    system_prompt: str,
    user_prompt: str,
    tool_schema: object,
    execute_tool: ToolExecutor,
    max_tool_calls: int = 200,
    timeout_seconds: float = 1800.0,
    config: ModelConfig | None = None,
    client: ModelClient | None = None,
) -> BaselineResult:
    """Run the stock loop once and return a harness-shaped invocation record."""
    resolved = config or ModelConfig.from_env(model=model_id)
    model = client or ModelClient(resolved, system_prompt)
    tools = _tool_list(tool_schema)
    recorder = _Recorder(execute_tool, max_tool_calls)

    started = time.monotonic()
    status: BaselineStatus = "completed"
    stop_reason = "end_turn"
    final_text = ""
    error_detail = ""
    try:
        final_text = await asyncio.wait_for(
            model.explore(
                phase=SCAFFOLD,
                content=user_prompt,
                tools=tools,
                on_tool=recorder,
                max_calls=max_tool_calls,
            ),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        status, stop_reason = "timed_out", "timeout"
    except ModelRefusal as exc:
        status, stop_reason = "refused", "refusal"
        error_detail = str(exc)[:200]
    except ModelAPIError as exc:
        status, stop_reason = "api_error", f"api_error:{exc.status_code}"
        error_detail = str(exc)[:200]
    else:
        if recorder.calls >= max_tool_calls and not final_text.strip():
            status, stop_reason = "tool_limit_exceeded", "max_tool_calls"

    latency_ms = round((time.monotonic() - started) * 1000)
    provider: Literal["anthropic", "openai", "google"] = "anthropic" if resolved.provider == "anthropic" else "openai"
    return BaselineResult(
        requested_model=model_id,
        response_model=resolved.model,
        provider=provider,
        final_text=final_text,
        status=status,
        stop_reason=stop_reason,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        events=tuple(recorder.events),
        usage=model.usage.harness_usage() | {"cost_usd": model.usage.cost_usd(resolved.pricing)},
        config={
            "model": resolved.model,
            "provider": resolved.provider,
            "effort": resolved.effort,
            "thinking": {"type": "model_default"},
            "scaffold": SCAFFOLD,
            "disclosure": DISCLOSURE,
            "ablations": [],
            "max_tool_calls": max_tool_calls,
            "timeout_seconds": timeout_seconds,
            "error": error_detail,
        },
        latency_ms=latency_ms,
        tool_calls=recorder.calls,
    )


def _is_failure(output: object) -> bool:
    """A gateway envelope with `ok: false` is an error the harness would also flag."""
    if not isinstance(output, Mapping):
        return False
    return cast(Mapping[str, Any], output).get("ok") is False


def _tool_list(tool_schema: object) -> list[dict[str, Any]]:
    if isinstance(tool_schema, Mapping):
        return [dict(cast(Mapping[str, Any], tool_schema))]
    return [dict(item) for item in cast(Sequence[Mapping[str, Any]], tool_schema)]
