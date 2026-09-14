"""Candidate factories emit harness-shaped invocation results the graders can pair with the provider trace."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from devsim.candidates import ScriptedCall, load_candidate, provider_for_model, scripted, stub
from devsim.harness import harness_module

TOOLS = [
    {"name": "provider_api", "description": "", "input_schema": {"type": "object"}},
    {"name": "provider_docs", "description": "", "input_schema": {"type": "object"}},
]


def _gateway_result(sequence: int, **overrides: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": True,
        "requested_provider": "stripe",
        "provider": "stripe",
        "method": "GET",
        "path": "/v1/customers",
        "status_code": 200,
        "headers": {},
        "body": {"object": "list", "data": []},
        "truncated": False,
        "error": None,
        "trace": {"sequence": sequence, "request_fingerprint": f"fp-{sequence}", "provider": "stripe"},
    }
    result.update(overrides)
    return result


async def _invoke(candidate: Any, execute_tool: Any, *, model_id: str = "deepseek-chat", **kwargs: Any) -> Any:
    return await candidate(
        model_id=model_id,
        system_prompt="SYSTEM",
        user_prompt="USER",
        tool_schema=TOOLS,
        execute_tool=execute_tool,
        max_tool_calls=kwargs.pop("max_tool_calls", 200),
        timeout_seconds=1800,
        api_effort=kwargs.pop("api_effort", "default"),
        thinking=kwargs.pop("thinking", "model_default"),
    )


@pytest.mark.arga
async def test_stub_completes_without_tool_calls_and_matches_profile_identity() -> None:
    async def execute_tool(_name: str, _input: dict[str, Any]) -> object:
        raise AssertionError("stub must not call tools")

    result = await _invoke(stub(), execute_tool)
    payload = result.as_dict()
    assert payload["requested_model"] == payload["response_model"] == "deepseek-chat"
    assert payload["provider"] == "openai"
    assert payload["status"] == "completed" and payload["stop_reason"] == "end_turn"
    assert payload["final_text"] == "{}"
    assert payload["system_prompt"] == "SYSTEM" and payload["user_prompt"] == "USER"
    assert payload["tool_calls"] == 0
    assert payload["usage"]["input_tokens"] == 0 and payload["usage"]["output_tokens"] == 0
    config = payload["config"]
    assert config["model"] == "deepseek-chat" and config["provider"] == "openai"
    assert config["effort"] == "default" and config["reasoning"] == {"effort": "default"}
    assert config["thinking"] == {"type": "model_default"}
    assert [event["type"] for event in payload["events"]] == ["invocation_started", "assistant_response"]
    json.dumps(payload)  # invocation.json must serialise


@pytest.mark.arga
async def test_provider_derivation_and_google_thinking_shape() -> None:
    assert provider_for_model("claude-opus-5") == "anthropic"
    assert provider_for_model("gemini-3.1-pro-preview") == "google"
    assert provider_for_model("gpt-5.6-sol") == "openai"

    async def execute_tool(_name: str, _input: dict[str, Any]) -> object:
        return {}

    anthropic = await _invoke(stub(), execute_tool, model_id="claude-opus-5", api_effort="high", thinking="adaptive")
    assert anthropic.provider == "anthropic" and anthropic.config["thinking"] == {"type": "adaptive"}
    google = await _invoke(stub(), execute_tool, model_id="gemini-3.5-flash", thinking="model_default")
    assert google.provider == "google" and google.config["thinking"] == "model_default"


@pytest.mark.arga
async def test_scripted_events_carry_executor_output_verbatim() -> None:
    seen: list[tuple[str, dict[str, Any]]] = []
    docs_result: dict[str, Any] = {"ok": True, "action": "search", "results": []}
    outputs: list[dict[str, Any]] = [_gateway_result(1), docs_result]

    async def execute_tool(name: str, tool_input: dict[str, Any]) -> object:
        seen.append((name, tool_input))
        return outputs[len(seen) - 1]

    calls = [
        ScriptedCall("provider_api", {"provider": "stripe", "method": "GET", "path": "/v1/customers"}),
        {"tool": "provider_docs", "input": {"provider": "stripe", "action": "search", "query": "customers"}},
    ]
    result = await _invoke(scripted(calls, '{"done": true}'), execute_tool)
    assert seen == [
        ("provider_api", {"provider": "stripe", "method": "GET", "path": "/v1/customers"}),
        ("provider_docs", {"provider": "stripe", "action": "search", "query": "customers"}),
    ]
    tool_events = [event for event in result.events if event["type"] == "tool_call"]
    assert len(tool_events) == 2 and result.tool_calls == 2
    first = tool_events[0]
    assert set(first) == {
        "type",
        "provider_call_index",
        "tool_use_id",
        "name",
        "arguments",
        "output",
        "is_error",
        "latency_ms",
    }
    assert first["provider_call_index"] == 1 and first["tool_use_id"] == "bp-1"
    assert first["name"] == "provider_api" and first["is_error"] is False
    assert first["output"] is outputs[0]  # verbatim: trace.sequence + request_fingerprint survive
    assert first["output"]["trace"]["sequence"] == 1 and first["output"]["trace"]["request_fingerprint"] == "fp-1"
    assert tool_events[1]["tool_use_id"] == "bp-2" and tool_events[1]["name"] == "provider_docs"
    assert result.events[0]["type"] == "invocation_started" and result.events[-1]["type"] == "assistant_response"
    assert result.status == "completed" and result.final_text == '{"done": true}'
    assert result.response_model == "deepseek-chat"


@pytest.mark.arga
async def test_scripted_marks_invalid_tools_and_executor_failures_as_errors() -> None:
    async def execute_tool(name: str, _input: dict[str, Any]) -> object:
        raise RuntimeError("boom")

    result = await _invoke(
        scripted(
            [
                {"tool": "provider_api", "input": {"provider": "slack", "method": "GET", "path": "/api/auth.test"}},
            ],
            "{}",
        ),
        execute_tool,
    )
    event = [event for event in result.events if event["type"] == "tool_call"][0]
    assert event["is_error"] is True
    assert event["output"] == {"error": {"type": "ToolExecutionError", "exception_type": "RuntimeError"}}
    assert result.status == "completed"  # like the stock adapter, a failed tool does not end the run

    with pytest.raises(ValueError):
        ScriptedCall.from_mapping({"tool": "not_a_tool", "input": {}})


@pytest.mark.arga
async def test_scripted_stops_at_the_tool_ceiling_and_propagates_infrastructure_errors() -> None:
    async def execute_tool(_name: str, _input: dict[str, Any]) -> object:
        return _gateway_result(1)

    call = {"tool": "provider_api", "input": {"provider": "stripe", "method": "GET", "path": "/v1/customers"}}
    limited = await _invoke(scripted([call, call], "{}"), execute_tool, max_tool_calls=1)
    assert limited.status == "tool_limit_exceeded" and limited.tool_calls == 1
    assert limited.events[-1]["type"] == "tool_limit_exceeded"

    retryable = harness_module("arga_twins_benchmark.errors").RetryableInfrastructureError

    async def failing(_name: str, _input: dict[str, Any]) -> object:
        raise retryable("twin unavailable")

    with pytest.raises(retryable):
        await _invoke(scripted([call], "{}"), failing)


def test_load_candidate_specs(tmp_path: Path) -> None:
    assert callable(load_candidate("stub"))
    script = tmp_path / "script.json"
    script.write_text(
        json.dumps(
            {
                "calls": [{"tool": "provider_api", "input": {"provider": "slack", "method": "GET", "path": "/x"}}],
                "final_text": {"a": 1},
            }
        )
    )
    assert callable(load_candidate(f"scripted:{script}"))
    assert callable(load_candidate("module:devsim.candidates:stub"))
    with pytest.raises(ValueError):
        load_candidate("nonsense")
    with pytest.raises(ValueError):
        load_candidate("module:devsim.candidates")
