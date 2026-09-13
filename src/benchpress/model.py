"""The model layer: one client, any model.

Benchpress talks to any OpenAI-compatible chat-completions endpoint (DeepSeek by default,
OpenAI, or a local server) and to the Anthropic Messages API through the same interface. Two
primitives are all the phases need:

- `emit(schema, content)` — a typed answer. The schema is forced as a tool call so the loop
  never parses prose; endpoints without forced tool choice fall back to JSON mode. One re-ask
  with the validation error, then `SchemaFailure` (the controller applies a conservative
  default).
- `explore(content, tools, on_tool)` — a bounded tool-use loop for the rare cases where a
  playbook has no operation for what a phase needs.

Every call is metered (tokens, cache hits, latency, cost) into `UsageTotals`.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypeVar, cast

import httpx
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)
ToolHandler = Callable[[str, dict[str, Any]], Awaitable[object]]

_RETRY_STATUSES = frozenset({408, 409, 429, 500, 502, 503, 504})
_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)
_TOOL_RESULT_LIMIT = 24_000


class ModelAPIError(RuntimeError):
    def __init__(self, status_code: int | None, message: str) -> None:
        super().__init__(f"model api error {status_code}: {message[:300]}")
        self.status_code = status_code
        self.message = message


class SchemaFailure(RuntimeError):
    """The model could not produce a valid instance of the requested schema."""


class ModelRefusal(RuntimeError):
    """The model refused the phase. Never fatal: the controller falls back."""


# --------------------------------------------------------------------------------------
# Configuration and metering
# --------------------------------------------------------------------------------------

_dotenv_loaded = False


def _load_dotenv_once() -> None:
    """Pick up `.env` from the working directory when keys are absent (dev convenience)."""
    global _dotenv_loaded
    if _dotenv_loaded or any(
        os.environ.get(name) for name in ("DEEPSEEK_API_KEY", "BENCHPRESS_API_KEY", "ANTHROPIC_API_KEY")
    ):
        _dotenv_loaded = True
        return
    _dotenv_loaded = True
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(os.path.join(os.getcwd(), ".env"))


@dataclass(frozen=True)
class Pricing:
    input_per_m: float = 1.32
    output_per_m: float = 3.96
    cache_read_per_m: float = 0.044
    source: str = "https://api-docs.deepseek.com/quick_start/pricing"


# DeepSeek prices are the PEAK rates from api-docs.deepseek.com/quick_start/pricing (verified
# 2026-09-13); off-peak is half. `deepseek-chat` is a legacy alias the API still accepts.
DEFAULT_PRICING: Mapping[str, Pricing] = {
    "deepseek-v4-pro": Pricing(1.32, 3.96, 0.044),
    "deepseek-flash": Pricing(0.30, 1.20, 0.006),
    "deepseek-chat": Pricing(1.32, 3.96, 0.044),
    "deepseek-reasoner": Pricing(1.32, 3.96, 0.044),
    "claude-opus-5": Pricing(5.0, 25.0, 0.5, "https://platform.claude.com/docs/en/about-claude/models"),
    "claude-sonnet-5": Pricing(3.0, 15.0, 0.3, "https://platform.claude.com/docs/en/about-claude/models"),
    "claude-fable-5": Pricing(10.0, 50.0, 1.0, "https://www.anthropic.com/claude/fable"),
    "claude-haiku-4-5-20251001": Pricing(1.0, 5.0, 0.1, "https://platform.claude.com/docs/en/about-claude/models"),
}


@dataclass(frozen=True)
class ModelConfig:
    model: str = "deepseek-v4-pro"
    provider: Literal["openai_compat", "anthropic"] = "openai_compat"
    api_base: str = "https://api.deepseek.com/v1"
    api_key: str = ""
    effort: str = "default"
    max_tokens: int = 8_000
    temperature: float | None = 0.0
    timeout_s: float = 240.0
    max_retries: int = 4
    pricing: Pricing = field(default_factory=Pricing)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, *, model: str | None = None) -> ModelConfig:
        if env is None:
            _load_dotenv_once()
        source = dict(os.environ if env is None else env)
        chosen = model or source.get("BENCHPRESS_MODEL", "deepseek-v4-pro")
        pricing = DEFAULT_PRICING.get(chosen)
        if chosen.startswith("claude"):
            return cls(
                model=chosen,
                provider="anthropic",
                api_base=source.get("ANTHROPIC_API_BASE", "https://api.anthropic.com"),
                api_key=source.get("ANTHROPIC_API_KEY", ""),
                effort=source.get("BENCHPRESS_EFFORT", "high"),
                max_tokens=int(source.get("BENCHPRESS_MAX_TOKENS", "16000")),
                temperature=None,
                pricing=pricing or Pricing(5.0, 25.0, 0.5),
            )
        api_key = source.get("BENCHPRESS_API_KEY") or source.get("DEEPSEEK_API_KEY") or source.get("OPENAI_API_KEY", "")
        return cls(
            model=chosen,
            api_base=source.get("BENCHPRESS_API_BASE", "https://api.deepseek.com/v1").rstrip("/"),
            api_key=api_key,
            effort=source.get("BENCHPRESS_EFFORT", "default"),
            max_tokens=int(source.get("BENCHPRESS_MAX_TOKENS", "8000")),
            temperature=None if "reasoner" in chosen else 0.0,
            pricing=pricing or Pricing(),
        )

    def describe(self) -> dict[str, Any]:
        return {"model": self.model, "provider": self.provider, "api_base": self.api_base, "effort": self.effort}


@dataclass
class UsageTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    reasoning_tokens: int = 0
    calls: int = 0
    latency_ms: int = 0

    def add(self, usage: object, latency_ms: int) -> None:
        self.calls += 1
        self.latency_ms += latency_ms
        if not isinstance(usage, Mapping):
            return
        raw = cast(Mapping[str, object], usage)
        self.input_tokens += _int(raw.get("prompt_tokens", raw.get("input_tokens")))
        self.output_tokens += _int(raw.get("completion_tokens", raw.get("output_tokens")))
        self.cache_read_input_tokens += _int(raw.get("prompt_cache_hit_tokens", raw.get("cache_read_input_tokens")))
        self.cache_creation_input_tokens += _int(raw.get("cache_creation_input_tokens"))
        details = raw.get("completion_tokens_details")
        if isinstance(details, Mapping):
            self.reasoning_tokens += _int(cast(Mapping[str, object], details).get("reasoning_tokens"))

    def cost_usd(self, pricing: Pricing) -> float:
        billable_input = max(0, self.input_tokens - self.cache_read_input_tokens)
        return round(
            billable_input / 1e6 * pricing.input_per_m
            + self.cache_read_input_tokens / 1e6 * pricing.cache_read_per_m
            + self.output_tokens / 1e6 * pricing.output_per_m,
            6,
        )

    def harness_usage(self) -> dict[str, Any]:
        """Anthropic-style keys, which is what the ArgaBench cost estimator reads."""
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "model_calls": self.calls,
            "model_latency_ms": self.latency_ms,
        }


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


# --------------------------------------------------------------------------------------
# Wire types
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    invalid: bool = False


@dataclass(frozen=True)
class ChatResponse:
    text: str
    tool_calls: tuple[ToolCall, ...]
    finish_reason: str
    usage: dict[str, Any]
    model: str
    raw: dict[str, Any]


class ModelTransport(Protocol):
    """Sends one chat-completions payload and returns an OpenAI-shaped response object."""

    async def chat(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    async def aclose(self) -> None: ...


# --------------------------------------------------------------------------------------
# Transports
# --------------------------------------------------------------------------------------


class OpenAICompatTransport:
    """POST {api_base}/chat/completions with bounded retries."""

    def __init__(self, config: ModelConfig, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(config.timeout_s, connect=20.0))

    async def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.config.api_base.rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {self.config.api_key}", "Content-Type": "application/json"}
        return await _post_with_retries(self._client, url, headers, payload, self.config.max_retries)

    async def list_models(self) -> list[str]:
        response = await self._client.get(
            f"{self.config.api_base.rstrip('/')}/models", headers={"Authorization": f"Bearer {self.config.api_key}"}
        )
        if response.status_code != 200:
            raise ModelAPIError(response.status_code, response.text)
        data = cast(dict[str, Any], response.json()).get("data", [])
        return sorted(str(item.get("id")) for item in cast(list[dict[str, Any]], data))

    async def aclose(self) -> None:
        await self._client.aclose()


class AnthropicTransport:
    """Translate the OpenAI-shaped payload to the Anthropic Messages API and back.

    Mirrors the request shape of ArgaBench's own Anthropic adapter (effort in `output_config`,
    adaptive thinking) so a Benchpress run on a Claude model stays comparable to the published
    baselines.
    """

    def __init__(self, config: ModelConfig, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(config.timeout_s, connect=20.0))

    async def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = _to_anthropic(payload, self.config)
        headers = {
            "x-api-key": self.config.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        url = f"{self.config.api_base.rstrip('/')}/v1/messages"
        raw = await _post_with_retries(self._client, url, headers, body, self.config.max_retries)
        return _from_anthropic(raw)

    async def aclose(self) -> None:
        await self._client.aclose()


async def _post_with_retries(
    client: httpx.AsyncClient, url: str, headers: Mapping[str, str], payload: Mapping[str, Any], max_retries: int
) -> dict[str, Any]:
    attempt = 0
    while True:
        try:
            response = await client.post(url, headers=dict(headers), json=payload)
        except httpx.TransportError as exc:
            if attempt >= max_retries:
                raise ModelAPIError(None, f"transport: {type(exc).__name__}: {exc}") from exc
            attempt += 1
            await asyncio.sleep(min(2.0**attempt, 20.0))
            continue
        if response.status_code in _RETRY_STATUSES and attempt < max_retries:
            attempt += 1
            retry_after = response.headers.get("retry-after", "")
            delay = float(retry_after) if retry_after.replace(".", "", 1).isdigit() else min(2.0**attempt, 20.0)
            await asyncio.sleep(min(delay, 30.0))
            continue
        if response.status_code >= 400:
            raise ModelAPIError(response.status_code, response.text)
        parsed: object = response.json()
        if not isinstance(parsed, dict):
            raise ModelAPIError(response.status_code, "response is not a JSON object")
        return cast(dict[str, Any], parsed)


def _to_anthropic(payload: Mapping[str, Any], config: ModelConfig) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    system_parts: list[str] = []
    for message in cast(Sequence[Mapping[str, Any]], payload.get("messages", [])):
        role = str(message.get("role"))
        if role == "system":
            system_parts.append(str(message.get("content", "")))
        elif role == "assistant":
            blocks: list[dict[str, Any]] = []
            if message.get("content"):
                blocks.append({"type": "text", "text": str(message["content"])})
            for call in cast(Sequence[Mapping[str, Any]], message.get("tool_calls", []) or []):
                function = cast(Mapping[str, Any], call.get("function", {}))
                arguments = function.get("arguments", "{}")
                parsed_args = _loads_object(arguments) if isinstance(arguments, str) else arguments
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": str(call.get("id")),
                        "name": str(function.get("name")),
                        "input": parsed_args,
                    }
                )
            messages.append({"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]})
        elif role == "tool":
            block: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": str(message.get("tool_call_id")),
                "content": str(message.get("content", "")),
            }
            if messages and messages[-1]["role"] == "user" and isinstance(messages[-1]["content"], list):
                cast(list[dict[str, Any]], messages[-1]["content"]).append(block)
            else:
                messages.append({"role": "user", "content": [block]})
        else:
            messages.append({"role": "user", "content": str(message.get("content", ""))})
    body: dict[str, Any] = {
        "model": config.model,
        "max_tokens": int(payload.get("max_tokens", config.max_tokens)),
        "messages": messages,
    }
    if system_parts:
        body["system"] = [{"type": "text", "text": "\n\n".join(system_parts), "cache_control": {"type": "ephemeral"}}]
    tools = cast(Sequence[Mapping[str, Any]], payload.get("tools") or [])
    if tools:
        body["tools"] = [
            {
                "name": str(cast(Mapping[str, Any], tool["function"])["name"]),
                "description": str(cast(Mapping[str, Any], tool["function"]).get("description", "")),
                "input_schema": cast(Mapping[str, Any], tool["function"]).get("parameters", {"type": "object"}),
            }
            for tool in tools
        ]
    choice = payload.get("tool_choice")
    if isinstance(choice, Mapping):
        function = cast(Mapping[str, Any], cast(Mapping[str, Any], choice).get("function", {}))
        body["tool_choice"] = {"type": "tool", "name": str(function.get("name"))}
    elif choice == "required":
        body["tool_choice"] = {"type": "any"}
    if config.effort not in {"", "default"}:
        body["output_config"] = {"effort": config.effort}
        body["thinking"] = {"type": "adaptive"}
    return body


def _from_anthropic(raw: Mapping[str, Any]) -> dict[str, Any]:
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in cast(Sequence[Mapping[str, Any]], raw.get("content", []) or []):
        if block.get("type") == "text":
            text_parts.append(str(block.get("text", "")))
        elif block.get("type") == "tool_use":
            tool_calls.append(
                {
                    "id": str(block.get("id")),
                    "type": "function",
                    "function": {"name": str(block.get("name")), "arguments": json.dumps(block.get("input", {}))},
                }
            )
    stop = str(raw.get("stop_reason", "end_turn"))
    finish = {"tool_use": "tool_calls", "end_turn": "stop", "max_tokens": "length", "refusal": "refusal"}.get(
        stop, stop
    )
    usage = cast(Mapping[str, Any], raw.get("usage", {}) or {})
    message: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts)}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": raw.get("id"),
        "model": raw.get("model"),
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {
            "prompt_tokens": _int(usage.get("input_tokens")),
            "completion_tokens": _int(usage.get("output_tokens")),
            "prompt_cache_hit_tokens": _int(usage.get("cache_read_input_tokens")),
            "cache_creation_input_tokens": _int(usage.get("cache_creation_input_tokens")),
        },
    }


# --------------------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------------------


class ModelClient:
    def __init__(self, config: ModelConfig, system_text: str, transport: ModelTransport | None = None) -> None:
        self.config = config
        self.system_text = system_text
        self.transport: ModelTransport = transport or (
            AnthropicTransport(config) if config.provider == "anthropic" else OpenAICompatTransport(config)
        )
        self.usage = UsageTotals()
        self.events: list[dict[str, Any]] = []
        self._forced_tool_unsupported = False

    # -- primitives ------------------------------------------------------------------

    async def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        phase: str = "",
        tools: Sequence[Mapping[str, Any]] | None = None,
        tool_choice: object | None = None,
        json_mode: bool = False,
        max_tokens: int | None = None,
    ) -> ChatResponse:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [dict(message) for message in messages],
            "max_tokens": max_tokens or self.config.max_tokens,
        }
        if self.config.temperature is not None:
            payload["temperature"] = self.config.temperature
        if tools:
            payload["tools"] = [dict(tool) for tool in tools]
            if tool_choice is not None:
                payload["tool_choice"] = tool_choice
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        started = time.monotonic()
        raw = await self.transport.chat(payload)
        latency = round((time.monotonic() - started) * 1000)
        response = _parse_response(raw)
        self.usage.add(response.usage, latency)
        self.events.append(
            {
                "phase": phase,
                "model": response.model or self.config.model,
                "finish_reason": response.finish_reason,
                "usage": response.usage,
                "latency_ms": latency,
                "tool_calls": len(response.tool_calls),
                "text_chars": len(response.text),
            }
        )
        if response.finish_reason == "refusal":
            raise ModelRefusal(f"{phase}: model refused")
        return response

    async def emit(self, *, phase: str, schema: type[T], content: str, retries: int = 1) -> T:
        """Return a validated `schema` instance for `content`, or raise SchemaFailure."""
        tool_name = f"emit_{phase}"
        tool = {
            "type": "function",
            "function": {
                "name": tool_name,
                "description": f"Emit the {schema.__name__} for the {phase} phase. Call exactly once.",
                "parameters": _json_schema(schema),
            },
        }
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_text},
            {"role": "user", "content": content},
        ]
        last_error = ""
        for attempt in range(retries + 1):
            payload_text = await self._emit_once(phase, messages, tool, tool_name, schema)
            try:
                data = _loads_object(payload_text)
                return schema.model_validate(data)
            except (ValidationError, ValueError) as exc:
                last_error = str(exc)[:2_000]
                if attempt >= retries:
                    break
                messages.append({"role": "assistant", "content": payload_text})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Your previous output failed validation against the required schema:\n"
                            f"{last_error}\n\nReply again with ONLY the corrected JSON object, nothing else."
                        ),
                    }
                )
        raise SchemaFailure(f"{phase}: {last_error}")

    async def _emit_once(
        self,
        phase: str,
        messages: list[dict[str, Any]],
        tool: dict[str, Any],
        tool_name: str,
        schema: type[BaseModel],
    ) -> str:
        if not self._forced_tool_unsupported:
            try:
                response = await self.complete(
                    messages,
                    phase=phase,
                    tools=[tool],
                    tool_choice={"type": "function", "function": {"name": tool_name}},
                )
            except ModelAPIError as exc:
                if exc.status_code is not None and 400 <= exc.status_code < 500:
                    self._forced_tool_unsupported = True
                else:
                    raise
            else:
                for call in response.tool_calls:
                    if call.name == tool_name and not call.invalid:
                        return json.dumps(call.arguments)
                if response.text.strip():
                    return _extract_json_text(response.text)
                self._forced_tool_unsupported = True
        schema_hint = json.dumps(_json_schema(schema), ensure_ascii=False)
        json_messages = list(messages)
        json_messages[-1] = {
            "role": json_messages[-1]["role"],
            "content": (
                f"{json_messages[-1]['content']}\n\nRespond with a single JSON object that validates against this JSON "
                f"schema (no prose, no code fences):\n{schema_hint}"
            ),
        }
        response = await self.complete(json_messages, phase=phase, json_mode=True)
        return _extract_json_text(response.text)

    async def explore(
        self,
        *,
        phase: str,
        content: str,
        tools: Sequence[Mapping[str, Any]],
        on_tool: ToolHandler,
        max_calls: int,
    ) -> str:
        """Bounded tool loop. Returns the model's final text."""
        converted = [_to_openai_tool(tool) for tool in tools]
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_text},
            {"role": "user", "content": content},
        ]
        calls = 0
        for _ in range(max_calls + 2):
            allow_tools = calls < max_calls
            response = await self.complete(messages, phase=phase, tools=converted if allow_tools else None)
            if not response.tool_calls or not allow_tools:
                return response.text
            messages.append(
                {
                    "role": "assistant",
                    "content": response.text,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
                        }
                        for call in response.tool_calls
                    ],
                }
            )
            for call in response.tool_calls:
                calls += 1
                if call.invalid:
                    result: object = {"error": {"type": "InvalidToolArguments"}}
                else:
                    try:
                        result = await on_tool(call.name, call.arguments)
                    except Exception as exc:  # noqa: BLE001 - the model must see failures as data
                        result = {"error": {"type": type(exc).__name__, "message": str(exc)[:500]}}
                messages.append({"role": "tool", "tool_call_id": call.id, "content": _tool_result_text(result)})
            if calls >= max_calls:
                messages.append(
                    {
                        "role": "user",
                        "content": "Tool budget exhausted. Answer now with what you have, in the requested format.",
                    }
                )
        return ""

    async def aclose(self) -> None:
        await self.transport.aclose()


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _parse_response(raw: Mapping[str, Any]) -> ChatResponse:
    choices = cast(Sequence[Mapping[str, Any]], raw.get("choices") or [])
    if not choices:
        raise ModelAPIError(None, f"no choices in response: {json.dumps(raw)[:300]}")
    choice = choices[0]
    message = cast(Mapping[str, Any], choice.get("message") or {})
    content = message.get("content")
    text = content if isinstance(content, str) else ""
    calls: list[ToolCall] = []
    for item in cast(Sequence[Mapping[str, Any]], message.get("tool_calls") or []):
        function = cast(Mapping[str, Any], item.get("function") or {})
        arguments_raw = function.get("arguments", "{}")
        invalid = False
        arguments: dict[str, Any] = {}
        if isinstance(arguments_raw, str):
            try:
                arguments = _loads_object(arguments_raw)
            except ValueError:
                invalid = True
        elif isinstance(arguments_raw, Mapping):
            arguments = dict(cast(Mapping[str, Any], arguments_raw))
        else:
            invalid = True
        calls.append(
            ToolCall(
                id=str(item.get("id") or f"call_{len(calls)}"),
                name=str(function.get("name", "")),
                arguments=arguments,
                invalid=invalid,
            )
        )
    finish = str(choice.get("finish_reason") or ("tool_calls" if calls else "stop"))
    usage = cast(Mapping[str, Any], raw.get("usage") or {})
    return ChatResponse(
        text=text,
        tool_calls=tuple(calls),
        finish_reason=finish,
        usage=dict(usage),
        model=str(raw.get("model") or ""),
        raw=dict(raw),
    )


def _loads_object(text: str) -> dict[str, Any]:
    parsed: object = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("expected a JSON object")
    return cast(dict[str, Any], parsed)


def _extract_json_text(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    match = _JSON_OBJECT.search(stripped)
    return match.group(0) if match else stripped


def _json_schema(schema: type[BaseModel]) -> dict[str, Any]:
    """Pydantic schema with `$ref`s inlined: some chat-completions endpoints reject `$defs`."""
    raw = schema.model_json_schema()
    defs = cast(dict[str, Any], raw.pop("$defs", {}) or {})
    return cast(dict[str, Any], _inline_refs(raw, defs))


def _inline_refs(node: object, defs: Mapping[str, Any], depth: int = 0) -> object:
    if depth > 12:
        return node
    if isinstance(node, Mapping):
        typed = cast(Mapping[str, Any], node)
        ref = typed.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            target = defs.get(ref.split("/")[-1])
            if isinstance(target, Mapping):
                merged = {key: value for key, value in typed.items() if key != "$ref"}
                merged.update(cast(Mapping[str, Any], target))
                return _inline_refs(merged, defs, depth + 1)
        return {key: _inline_refs(value, defs, depth + 1) for key, value in typed.items()}
    if isinstance(node, list):
        return [_inline_refs(item, defs, depth + 1) for item in cast(list[object], node)]
    return node


def _to_openai_tool(tool: Mapping[str, Any]) -> dict[str, Any]:
    if tool.get("type") == "function" and "function" in tool:
        return dict(tool)
    parameters = tool.get("input_schema", tool.get("parameters", {"type": "object", "properties": {}}))
    return {
        "type": "function",
        "function": {
            "name": str(tool.get("name")),
            "description": str(tool.get("description", "")),
            "parameters": parameters,
        },
    }


def _tool_result_text(result: object) -> str:
    try:
        rendered = json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        rendered = json.dumps({"error": {"type": "NonJsonToolResult"}})
    if len(rendered) > _TOOL_RESULT_LIMIT:
        return rendered[:_TOOL_RESULT_LIMIT] + '…"truncated":true}'
    return rendered


# --------------------------------------------------------------------------------------
# Smoke test: `python -m benchpress.model --smoke`
# --------------------------------------------------------------------------------------


async def _smoke() -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    from benchpress.context import TaskFrame

    config = ModelConfig.from_env()
    if not config.api_key:
        print("no API key in env (DEEPSEEK_API_KEY / BENCHPRESS_API_KEY / ANTHROPIC_API_KEY)")
        return 2
    print(json.dumps(config.describe()))
    client = ModelClient(config, "You are a careful operations analyst. Answer only through the requested tool.")
    if isinstance(client.transport, OpenAICompatTransport):
        try:
            models = await client.transport.list_models()
            print("models:", ", ".join(models[:12]))
        except ModelAPIError as exc:
            print("list_models failed:", exc)
    prompt = (
        "Dana Reyes from support posted in the #billing-desk Slack channel: Harbor Lane Bakery asked for invoices "
        "to go to finance@harborlane.example. A similarly named prospect exists. Do not send external mail or "
        "create charges unless clearly authorized. Keep the channel informed."
    )
    frame = await client.emit(
        phase="orient", schema=TaskFrame, content=f"Parse this request into a TaskFrame:\n\n{prompt}"
    )
    print(frame.model_dump_json(indent=2))
    print("usage:", json.dumps(client.usage.harness_usage()), "cost_usd:", client.usage.cost_usd(config.pricing))
    await client.aclose()
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(asyncio.run(_smoke()) if "--smoke" in sys.argv else 0)
