"""Model layer contract, with a scripted transport and no network."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any

import pytest

from benchpress.context import Frozen, TaskFrame
from benchpress.model import (
    ModelAPIError,
    ModelClient,
    ModelConfig,
    ModelRefusal,
    Pricing,
    SchemaFailure,
    UsageTotals,
    _extract_json_text,
    _from_anthropic,
    _json_schema,
    _to_anthropic,
)


class Toy(Frozen):
    name: str
    count: int


class Nested(Frozen):
    items: tuple[Toy, ...]
    note: str = ""


def _tool_response(name: str, arguments: dict[str, Any], *, usage: dict[str, int] | None = None) -> dict[str, Any]:
    return {
        "model": "stub-model",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(arguments)},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": usage or {"prompt_tokens": 100, "completion_tokens": 20, "prompt_cache_hit_tokens": 40},
    }


def _text_response(text: str, finish: str = "stop") -> dict[str, Any]:
    return {
        "model": "stub-model",
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


class ScriptedTransport:
    def __init__(
        self, steps: Sequence[dict[str, Any] | Callable[[dict[str, Any]], dict[str, Any]] | Exception]
    ) -> None:
        self.steps = list(steps)
        self.payloads: list[dict[str, Any]] = []

    async def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.payloads.append(payload)
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        if callable(step):
            return step(payload)
        return step

    async def aclose(self) -> None:
        return None


def _client(steps: Sequence[Any]) -> tuple[ModelClient, ScriptedTransport]:
    transport = ScriptedTransport(steps)
    return ModelClient(ModelConfig(model="stub-model"), "system text", transport=transport), transport


async def test_emit_returns_validated_instance_from_forced_tool_call() -> None:
    client, transport = _client([_tool_response("emit_orient", {"name": "a", "count": 2})])
    result = await client.emit(phase="orient", schema=Toy, content="parse")
    assert result == Toy(name="a", count=2)
    payload = transport.payloads[0]
    assert payload["tool_choice"] == {"type": "function", "function": {"name": "emit_orient"}}
    assert payload["messages"][0] == {"role": "system", "content": "system text"}
    assert payload["tools"][0]["function"]["parameters"]["properties"]["count"]["type"] == "integer"


async def test_emit_reasks_once_with_validation_error_then_succeeds() -> None:
    client, transport = _client(
        [
            _tool_response("emit_orient", {"name": "a", "count": "many"}),
            _tool_response("emit_orient", {"name": "a", "count": 3}),
        ]
    )
    result = await client.emit(phase="orient", schema=Toy, content="parse")
    assert result.count == 3
    reask = transport.payloads[1]["messages"][-1]["content"]
    assert "failed validation" in reask and "count" in reask


async def test_emit_gives_up_after_retries() -> None:
    client, _ = _client([_tool_response("emit_x", {"name": "a"}), _tool_response("emit_x", {"name": "b"})])
    with pytest.raises(SchemaFailure):
        await client.emit(phase="x", schema=Toy, content="parse", retries=1)


async def test_emit_retries_an_empty_reply_without_a_correction_turn() -> None:
    client, transport = _client([_text_response(""), _text_response('{"name": "z", "count": 1}')])
    result = await client.emit(phase="x", schema=Toy, content="parse")
    assert result == Toy(name="z", count=1)
    assert transport.payloads[1]["response_format"] == {"type": "json_object"}
    assert all(m.get("role") != "assistant" for m in transport.payloads[1]["messages"])


async def test_refusal_raises_model_refusal() -> None:
    client, _ = _client([_text_response("no", finish="refusal")])
    with pytest.raises(ModelRefusal):
        await client.emit(phase="x", schema=Toy, content="parse")


async def test_forced_tool_choice_unsupported_falls_back_to_json_mode() -> None:
    client, transport = _client(
        [
            ModelAPIError(400, "tool_choice not supported"),
            _text_response('```json\n{"name": "z", "count": 1}\n```'),
            _text_response('{"name": "y", "count": 9}'),
        ]
    )
    first = await client.emit(phase="x", schema=Toy, content="parse")
    assert first == Toy(name="z", count=1)
    assert transport.payloads[1]["response_format"] == {"type": "json_object"}
    assert "schema" in transport.payloads[1]["messages"][-1]["content"]
    second = await client.emit(phase="x", schema=Toy, content="again")
    assert second.count == 9
    assert "tool_choice" not in transport.payloads[2]


async def test_explore_runs_tools_until_final_text_and_respects_budget() -> None:
    seen: list[tuple[str, dict[str, Any]]] = []

    async def on_tool(name: str, arguments: dict[str, Any]) -> object:
        seen.append((name, arguments))
        return {"ok": True, "body": {"echo": arguments}}

    client, transport = _client(
        [
            _tool_response("provider_api", {"provider": "slack", "method": "GET", "path": "/api/users.list"}),
            _tool_response("provider_api", {"provider": "slack", "method": "GET", "path": "/api/conversations.list"}),
            _text_response("done"),
        ]
    )
    tools: list[dict[str, Any]] = [
        {"name": "provider_api", "description": "d", "input_schema": {"type": "object", "properties": {}}}
    ]
    text = await client.explore(phase="p2", content="look", tools=tools, on_tool=on_tool, max_calls=2)
    assert text == "done"
    assert [name for name, _ in seen] == ["provider_api", "provider_api"]
    assert transport.payloads[0]["tools"][0]["function"]["name"] == "provider_api"
    tool_message = transport.payloads[1]["messages"][-1]
    assert (
        tool_message["role"] == "tool"
        and json.loads(tool_message["content"])["body"]["echo"]["path"] == "/api/users.list"
    )
    assert "tools" not in transport.payloads[2], "no tools offered once the budget is spent"
    assert "Tool budget exhausted" in transport.payloads[2]["messages"][-1]["content"]


async def test_usage_is_summed_and_costed() -> None:
    client, _ = _client(
        [_tool_response("emit_x", {"name": "a", "count": 1}), _tool_response("emit_x", {"name": "a", "count": 1})]
    )
    await client.emit(phase="x", schema=Toy, content="1")
    await client.emit(phase="x", schema=Toy, content="2")
    assert client.usage.calls == 2
    assert client.usage.input_tokens == 200 and client.usage.cache_read_input_tokens == 80
    pricing = Pricing(input_per_m=1.0, output_per_m=10.0, cache_read_per_m=0.1)
    assert client.usage.cost_usd(pricing) == pytest.approx((120 / 1e6) * 1.0 + (80 / 1e6) * 0.1 + (40 / 1e6) * 10.0)
    assert client.usage.harness_usage()["output_tokens"] == 40
    assert len(client.events) == 2 and client.events[0]["phase"] == "x"


def test_usage_accepts_anthropic_keys() -> None:
    totals = UsageTotals()
    totals.add(
        {"input_tokens": 5, "output_tokens": 7, "cache_read_input_tokens": 2, "cache_creation_input_tokens": 1}, 10
    )
    assert (
        totals.input_tokens,
        totals.output_tokens,
        totals.cache_read_input_tokens,
        totals.cache_creation_input_tokens,
    ) == (
        5,
        7,
        2,
        1,
    )


def test_json_schema_inlines_refs() -> None:
    schema = _json_schema(Nested)
    assert "$defs" not in schema
    assert schema["properties"]["items"]["items"]["properties"]["count"]["type"] == "integer"


def test_extract_json_text_handles_fences_and_prose() -> None:
    assert _extract_json_text('Sure:\n```json\n{"a": 1}\n```') == '{"a": 1}'
    assert _extract_json_text('{"a": {"b": 2}} trailing') == '{"a": {"b": 2}}'


def test_anthropic_translation_round_trip() -> None:
    config = ModelConfig(model="claude-opus-5", provider="anthropic", effort="high")
    payload = {
        "model": "claude-opus-5",
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "t1", "type": "function", "function": {"name": "provider_api", "arguments": '{"a":1}'}}
                ],
            },
            {"role": "tool", "tool_call_id": "t1", "content": '{"ok":true}'},
        ],
        "tools": [
            {
                "type": "function",
                "function": {"name": "provider_api", "description": "d", "parameters": {"type": "object"}},
            }
        ],
        "tool_choice": {"type": "function", "function": {"name": "provider_api"}},
        "max_tokens": 100,
    }
    body = _to_anthropic(payload, config)
    assert body["system"][0]["text"] == "sys" and body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["messages"][1]["content"][0]["type"] == "tool_use"
    assert body["messages"][2]["content"][0] == {"type": "tool_result", "tool_use_id": "t1", "content": '{"ok":true}'}
    assert body["tools"][0]["input_schema"] == {"type": "object"} and body["tool_choice"] == {
        "type": "tool",
        "name": "provider_api",
    }
    assert body["output_config"] == {"effort": "high"} and body["thinking"] == {"type": "adaptive"}
    back = _from_anthropic(
        {
            "content": [
                {"type": "text", "text": "ok"},
                {"type": "tool_use", "id": "x", "name": "provider_api", "input": {"q": 1}},
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 3, "output_tokens": 4, "cache_read_input_tokens": 1},
            "model": "claude-opus-5",
        }
    )
    assert back["choices"][0]["finish_reason"] == "tool_calls"
    assert json.loads(back["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]) == {"q": 1}
    assert back["usage"]["prompt_cache_hit_tokens"] == 1


def test_config_from_env_picks_provider() -> None:
    deepseek = ModelConfig.from_env({"DEEPSEEK_API_KEY": "k", "BENCHPRESS_MODEL": "deepseek-chat"})
    assert deepseek.provider == "openai_compat" and deepseek.api_key == "k" and deepseek.temperature == 0.0
    claude = ModelConfig.from_env({"ANTHROPIC_API_KEY": "a", "BENCHPRESS_MODEL": "claude-sonnet-5"})
    assert claude.provider == "anthropic" and claude.effort == "high" and claude.temperature is None
    assert TaskFrame().verify_required is True
