"""Anthropic Messages API contract for `AnthropicTransport`, offline over `httpx.MockTransport`.

No key, no network: every request the transport would send to `POST /v1/messages` is captured and
checked against the Messages API shape, and every response is a canned Messages API payload.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from benchpress.context import Frozen
from benchpress.model import (
    DEFAULT_PRICING,
    AnthropicTransport,
    ModelAPIError,
    ModelClient,
    ModelConfig,
    ModelRefusal,
    _to_anthropic,
)

API_BASE = "https://api.anthropic.test"
SYSTEM = "You are a careful operations analyst."


class Toy(Frozen):
    name: str
    count: int


class Recorder:
    """A MockTransport handler that records requests and replays canned responses in order."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = responses
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0)

    def body(self, index: int) -> dict[str, Any]:
        parsed: dict[str, Any] = json.loads(self.requests[index].content)
        return parsed


def _usage(
    input_tokens: int = 12, output_tokens: int = 7, cache_read: int = 0, cache_creation: int = 0
) -> dict[str, int]:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_creation,
    }


def _message(
    content: list[dict[str, Any]], stop_reason: str, usage: dict[str, int] | None = None, model: str = "claude-opus-5"
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "msg_01",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": usage or _usage(),
        },
    )


def _error(status: int, error_type: str, message: str, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(
        status, json={"type": "error", "error": {"type": error_type, "message": message}}, headers=headers or {}
    )


def _thinking(signature: str) -> dict[str, Any]:
    # Current models default to `display: "omitted"`: the thinking text is empty, the signature is not.
    return {"type": "thinking", "thinking": "", "signature": signature}


def _tool_use(tool_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": arguments}


def _client(
    recorder: Recorder, *, model: str = "claude-opus-5", effort: str = "high", max_retries: int = 4
) -> ModelClient:
    # temperature keeps its 0.0 default on purpose: the transport must never forward it.
    config = ModelConfig(
        model=model,
        provider="anthropic",
        api_base=API_BASE,
        api_key="sk-ant-test",
        effort=effort,
        max_tokens=16_000,
        max_retries=max_retries,
    )
    transport = AnthropicTransport(config, client=httpx.AsyncClient(transport=httpx.MockTransport(recorder)))
    return ModelClient(config, SYSTEM, transport=transport)


# -- request shape ------------------------------------------------------------------------


async def test_emit_sends_a_messages_api_request_with_forced_tool_use() -> None:
    recorder = Recorder(
        [_message([_thinking("sig-1"), _tool_use("toolu_01", "emit_orient", {"name": "a", "count": 2})], "tool_use")]
    )
    client = _client(recorder)
    result = await client.emit(phase="orient", schema=Toy, content="parse this")
    await client.aclose()

    assert result == Toy(name="a", count=2)
    request = recorder.requests[0]
    assert request.method == "POST" and str(request.url) == f"{API_BASE}/v1/messages"
    assert request.headers["x-api-key"] == "sk-ant-test"
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert request.headers["content-type"] == "application/json"
    assert "authorization" not in request.headers

    body = recorder.body(0)
    assert body["model"] == "claude-opus-5" and body["max_tokens"] == 16_000
    assert body["system"] == [{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}]
    assert body["messages"] == [{"role": "user", "content": "parse this"}], "system is top-level, never a message"
    (tool,) = body["tools"]
    assert set(tool) == {"name", "description", "input_schema"}
    assert tool["name"] == "emit_orient" and tool["input_schema"]["properties"]["count"]["type"] == "integer"
    assert body["tool_choice"] == {"type": "tool", "name": "emit_orient"}
    assert body["thinking"] == {"type": "adaptive"} and body["output_config"] == {"effort": "high"}
    for absent in ("temperature", "top_p", "top_k", "response_format", "stream"):
        assert absent not in body


async def test_legacy_models_get_no_adaptive_thinking_or_effort() -> None:
    for model in ("claude-haiku-4-5-20251001", "claude-haiku-4-5", "claude-sonnet-4-5", "claude-opus-4-1-20250805"):
        recorder = Recorder([_message([_tool_use("toolu_01", "emit_x", {"name": "h", "count": 1})], "tool_use")])
        client = _client(recorder, model=model, effort="high")
        assert await client.emit(phase="x", schema=Toy, content="go") == Toy(name="h", count=1)
        body = recorder.body(0)
        assert "thinking" not in body and "output_config" not in body, model
    modern = ModelConfig(model="claude-opus-4-6", provider="anthropic", effort="high")
    assert _to_anthropic({"messages": [{"role": "user", "content": "x"}]}, modern)["thinking"] == {"type": "adaptive"}


# -- response shape -----------------------------------------------------------------------


async def test_text_json_reply_is_parsed_and_thinking_blocks_are_ignored() -> None:
    recorder = Recorder(
        [_message([_thinking("sig"), {"type": "text", "text": '{"name": "t", "count": 4}'}], "end_turn")]
    )
    client = _client(recorder)
    assert await client.emit(phase="x", schema=Toy, content="go") == Toy(name="t", count=4)
    event = client.events[0]
    assert event["finish_reason"] == "stop" and event["text_chars"] == len('{"name": "t", "count": 4}')


@pytest.mark.parametrize(
    ("stop_reason", "finish_reason"),
    [("tool_use", "tool_calls"), ("end_turn", "stop"), ("max_tokens", "length"), ("pause_turn", "pause_turn")],
)
async def test_stop_reasons_map_to_finish_reasons(stop_reason: str, finish_reason: str) -> None:
    content = [_tool_use("toolu_01", "t", {})] if stop_reason == "tool_use" else [{"type": "text", "text": "hi"}]
    recorder = Recorder([_message(content, stop_reason)])
    client = _client(recorder)
    response = await client.complete([{"role": "user", "content": "x"}], phase="p")
    assert response.finish_reason == finish_reason


async def test_refusal_stop_reason_raises_model_refusal() -> None:
    recorder = Recorder([_message([], "refusal")])
    client = _client(recorder)
    with pytest.raises(ModelRefusal):
        await client.emit(phase="x", schema=Toy, content="go")


async def test_thinking_only_max_tokens_reply_is_retried_with_a_larger_budget() -> None:
    # An empty forced-tool reply first drops to JSON mode at the same budget; a second empty reply doubles it.
    recorder = Recorder(
        [
            _message([_thinking("sig-a")], "max_tokens"),
            _message([_thinking("sig-b")], "max_tokens"),
            _message([{"type": "text", "text": '{"name": "m", "count": 8}'}], "end_turn"),
        ]
    )
    client = _client(recorder)
    assert await client.emit(phase="x", schema=Toy, content="go") == Toy(name="m", count=8)
    assert [recorder.body(index)["max_tokens"] for index in range(3)] == [16_000, 16_000, 32_000]
    assert "tool_choice" in recorder.body(0) and "tools" not in recorder.body(2)
    assert all(message["role"] == "user" for message in recorder.body(2)["messages"]), "no empty assistant turn"
    assert [event["finish_reason"] for event in client.events] == ["length", "length", "stop"]


async def test_usage_counts_cached_tokens_once_and_prices_cache_reads_and_writes() -> None:
    usage = _usage(input_tokens=50, output_tokens=20, cache_read=1_000, cache_creation=200)
    recorder = Recorder([_message([_tool_use("toolu_01", "emit_x", {"name": "u", "count": 1})], "tool_use", usage)])
    client = _client(recorder)
    await client.emit(phase="x", schema=Toy, content="go")
    totals = client.usage
    assert totals.input_tokens == 1_250, "whole prompt: uncached + cache read + cache write"
    assert (totals.cache_read_input_tokens, totals.cache_creation_input_tokens, totals.output_tokens) == (
        1_000,
        200,
        20,
    )
    # Opus 5: $5 input, $0.50 cache read, $6.25 cache write, $25 output per MTok.
    expected = (50 * 5.0 + 1_000 * 0.5 + 200 * 6.25 + 20 * 25.0) / 1e6
    assert totals.cost_usd(DEFAULT_PRICING["claude-opus-5"]) == pytest.approx(expected)
    assert totals.harness_usage()["cache_read_input_tokens"] == 1_000


# -- tool loop ----------------------------------------------------------------------------


async def test_explore_round_trips_tool_results_and_thinking_blocks_over_the_wire() -> None:
    first_turn = [
        _thinking("sig-turn-1"),
        {"type": "text", "text": "Looking up both."},
        _tool_use("toolu_1a", "provider_api", {"path": "/users"}),
        _tool_use("toolu_1b", "provider_api", {"path": "/channels"}),
    ]
    second_turn = [_thinking("sig-turn-2"), _tool_use("toolu_2", "provider_api", {"path": "/history"})]
    recorder = Recorder(
        [
            _message(first_turn, "tool_use"),
            _message(second_turn, "tool_use"),
            _message([{"type": "text", "text": "done"}], "end_turn"),
        ]
    )
    seen: list[dict[str, Any]] = []

    async def on_tool(name: str, arguments: dict[str, Any]) -> object:
        seen.append(arguments)
        if arguments["path"] == "/channels":
            raise RuntimeError("boom")
        return {"ok": True, "path": arguments["path"]}

    client = _client(recorder)
    tools: list[dict[str, Any]] = [
        {"name": "provider_api", "description": "Call a provider.", "input_schema": {"type": "object"}}
    ]
    text = await client.explore(phase="p2", content="look", tools=tools, on_tool=on_tool, max_calls=3)

    assert text == "done"
    assert [arguments["path"] for arguments in seen] == ["/users", "/channels", "/history"]
    first = recorder.body(0)
    assert first["tools"] == [
        {"name": "provider_api", "description": "Call a provider.", "input_schema": {"type": "object"}}
    ]
    assert "tool_choice" not in first

    second = recorder.body(1)
    assert second["messages"][0] == {"role": "user", "content": "look"}
    assert second["messages"][1] == {"role": "assistant", "content": first_turn}, "turn replayed verbatim"
    results = second["messages"][2]
    assert results["role"] == "user" and len(second["messages"]) == 3, "parallel results share one user turn"
    assert [block["tool_use_id"] for block in results["content"]] == ["toolu_1a", "toolu_1b"]
    assert all(block["type"] == "tool_result" and isinstance(block["content"], str) for block in results["content"])
    assert json.loads(results["content"][0]["content"]) == {"ok": True, "path": "/users"}
    assert json.loads(results["content"][1]["content"])["error"]["type"] == "RuntimeError"

    final = recorder.body(2)
    assert final["messages"][3] == {"role": "assistant", "content": second_turn}
    closing = final["messages"][4]["content"]
    assert closing[0]["type"] == "tool_result" and closing[0]["tool_use_id"] == "toolu_2"
    assert closing[1]["type"] == "text" and "Tool budget exhausted" in closing[1]["text"]
    assert final["tools"] == [{"name": "provider_api", "input_schema": {"type": "object"}}]
    assert final["tool_choice"] == {"type": "none"}, "history with tool blocks declares tools but allows no call"


# -- errors -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "error_type"), [(429, "rate_limit_error"), (529, "overloaded_error"), (500, "api_error")]
)
async def test_transient_errors_are_retried(status: int, error_type: str) -> None:
    recorder = Recorder(
        [
            _error(status, error_type, "try again", headers={"retry-after": "0"}),
            _message([_tool_use("toolu_01", "emit_x", {"name": "r", "count": 1})], "tool_use"),
        ]
    )
    client = _client(recorder)
    assert await client.emit(phase="x", schema=Toy, content="go") == Toy(name="r", count=1)
    assert len(recorder.requests) == 2 and recorder.body(0) == recorder.body(1)


async def test_persistent_overload_surfaces_after_bounded_retries() -> None:
    recorder = Recorder([_error(529, "overloaded_error", "Overloaded", headers={"retry-after": "0"}) for _ in range(3)])
    client = _client(recorder, max_retries=2)
    with pytest.raises(ModelAPIError) as caught:
        await client.complete([{"role": "user", "content": "x"}])
    assert caught.value.status_code == 529 and "overloaded_error" in caught.value.message
    assert len(recorder.requests) == 3


async def test_invalid_request_is_not_retried_and_is_surfaced_clearly() -> None:
    message = "max_tokens: Field required"
    recorder = Recorder([_error(400, "invalid_request_error", message)])
    client = _client(recorder)
    with pytest.raises(ModelAPIError) as caught:
        await client.complete([{"role": "user", "content": "x"}])
    assert len(recorder.requests) == 1
    assert caught.value.status_code == 400
    assert "invalid_request_error" in str(caught.value) and message in str(caught.value)


async def test_emit_falls_back_once_then_surfaces_a_repeated_invalid_request() -> None:
    recorder = Recorder(
        [
            _error(400, "invalid_request_error", "tool_choice: not supported for this model"),
            _error(400, "invalid_request_error", "model: unknown model"),
        ]
    )
    client = _client(recorder, model="claude-fable-5-1")
    with pytest.raises(ModelAPIError) as caught:
        await client.emit(phase="x", schema=Toy, content="go")
    assert caught.value.status_code == 400 and "unknown model" in caught.value.message
    assert recorder.body(0)["tool_choice"] == {"type": "tool", "name": "emit_x"}
    assert "tool_choice" not in recorder.body(1) and "tools" not in recorder.body(1)


# -- configuration ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "input_per_m", "output_per_m", "effort"),
    [
        ("claude-opus-5", 5.0, 25.0, "high"),
        ("claude-sonnet-5", 2.0, 10.0, "high"),
        ("claude-haiku-4-5-20251001", 1.0, 5.0, "default"),
        ("claude-haiku-4-5", 1.0, 5.0, "default"),
    ],
)
def test_current_claude_ids_resolve_to_anthropic_with_pricing(
    model: str, input_per_m: float, output_per_m: float, effort: str
) -> None:
    config = ModelConfig.from_env({"ANTHROPIC_API_KEY": "sk-ant-test"}, model=model)
    assert config.provider == "anthropic" and config.api_base == "https://api.anthropic.com"
    assert config.api_key == "sk-ant-test" and config.temperature is None and config.effort == effort
    assert config.pricing is DEFAULT_PRICING[model]
    assert (config.pricing.input_per_m, config.pricing.output_per_m) == (input_per_m, output_per_m)
    assert config.pricing.cache_read_per_m == pytest.approx(input_per_m * 0.1)
    assert config.pricing.cache_write_per_m == pytest.approx(input_per_m * 1.25)
    body = _to_anthropic({"messages": [{"role": "user", "content": "x"}], "temperature": 0.0}, config)
    assert "temperature" not in body
    assert ("thinking" in body) is (effort == "high")


def test_unknown_claude_ids_still_route_to_anthropic_with_opus_tier_pricing() -> None:
    config = ModelConfig.from_env({}, model="claude-future-9")
    assert config.provider == "anthropic" and config.effort == "high"
    assert (config.pricing.input_per_m, config.pricing.output_per_m) == (5.0, 25.0)
