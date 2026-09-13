"""RealAppGateway is a faithful stand-in for the harness provider gateway.

No network: every test routes through `httpx.MockTransport`. Provider names and paths are
the real ones (`slack`, `/api/chat.postMessage`); every entity is invented.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from benchpress.realapp import (
    AuthError,
    GmailTokenProvider,
    RealAppConfig,
    RealAppGateway,
    ScratchGuardError,
    config_from_env,
    gateway_from_env,
    gmail_token_provider,
    request_fingerprints,
)

SCRATCH_ENV = {"BENCHPRESS_SCRATCH_OK": "1"}
SLACK_TOKEN = "xoxb-unit-test-token-0123456789"
STRIPE_TEST_KEY = "sk_test_unit_0123456789abcdef"
HEX64 = re.compile(r"^[0-9a-f]{64}$")

# ProviderTraceRecord.to_dict() field set (gateway.py:195-226).
TRACE_KEYS = (
    "sequence",
    "started_at",
    "requested_provider",
    "provider",
    "method",
    "path",
    "operation",
    "operation_type",
    "status_code",
    "latency_ms",
    "response_bytes",
    "truncated",
    "error",
    "request_fingerprint",
    "action_fingerprint",
    "attempt_fingerprint",
)
# Result envelope keys (gateway.py:448-460).
ENVELOPE_KEYS = (
    "ok",
    "requested_provider",
    "provider",
    "method",
    "path",
    "status_code",
    "headers",
    "body",
    "truncated",
    "error",
    "trace",
)

Responder = Callable[[httpx.Request], httpx.Response]


def _ok(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True})


class Recorder:
    """A MockTransport handler that keeps every request it saw."""

    def __init__(self, responder: Responder = _ok) -> None:
        self.requests: list[httpx.Request] = []
        self._responder = responder

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._responder(request)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


class FakeSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def slack_config(token: str = SLACK_TOKEN, base_url: str = "https://slack.com") -> RealAppConfig:
    return RealAppConfig.for_provider("slack", base_url=base_url, token=token)


def stripe_config(key: str = STRIPE_TEST_KEY, base_url: str = "https://api.stripe.com") -> RealAppConfig:
    return RealAppConfig.for_provider("stripe", base_url=base_url, token=key)


def make_gateway(recorder: Recorder, *configs: RealAppConfig, **kwargs: Any) -> RealAppGateway:
    return RealAppGateway(list(configs) or [slack_config()], env=SCRATCH_ENV, transport=recorder.transport, **kwargs)


# -- envelope and trace ---------------------------------------------------------------------


async def test_success_envelope_and_trace_match_the_harness_shape() -> None:
    recorder = Recorder(lambda _request: httpx.Response(200, json={"ok": True, "channels": []}))
    gateway = make_gateway(recorder)
    result = await gateway.execute_tool(
        "provider_api",
        {
            "provider": "slack",
            "method": "GET",
            "path": "/api/conversations.list?limit=200",
            "query": {"types": "public_channel"},
        },
    )
    assert tuple(result) == ENVELOPE_KEYS
    assert result["ok"] is True
    assert result["status_code"] == 200
    assert result["requested_provider"] == "slack" and result["provider"] == "slack"
    assert result["method"] == "GET"
    assert result["path"] == "/api/conversations.list?limit=200&types=public_channel"
    assert result["body"] == {"ok": True, "channels": []}
    assert result["truncated"] is False and result["error"] is None
    assert "content-type" in result["headers"]

    trace = result["trace"]
    assert tuple(trace) == TRACE_KEYS
    assert trace["sequence"] == 1
    assert trace["provider"] == "slack" and trace["requested_provider"] == "slack"
    assert trace["status_code"] == 200 and trace["error"] is None
    assert trace["response_bytes"] == len(json.dumps({"ok": True, "channels": []}).replace(" ", ""))
    assert HEX64.match(trace["request_fingerprint"]) and HEX64.match(trace["action_fingerprint"])
    assert HEX64.match(trace["attempt_fingerprint"])
    assert gateway.trace == (trace,)

    request = recorder.requests[0]
    assert request.url == httpx.URL("https://slack.com/api/conversations.list?limit=200&types=public_channel")
    assert request.headers["authorization"] == f"Bearer {SLACK_TOKEN}"
    assert request.headers["user-agent"].startswith("benchpress")
    await gateway.aclose()


async def test_non_2xx_response_is_ok_false_but_still_carries_the_body() -> None:
    recorder = Recorder(lambda _request: httpx.Response(404, json={"error": {"message": "No such customer"}}))
    gateway = make_gateway(recorder, stripe_config())
    result = await gateway.execute_tool(
        "provider_api", {"provider": "stripe", "method": "GET", "path": "/v1/customers/cus_x"}
    )
    assert result["ok"] is False and result["status_code"] == 404
    assert result["body"] == {"error": {"message": "No such customer"}}
    assert result["error"] is None
    assert result["trace"]["status_code"] == 404
    await gateway.aclose()


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/api",
        "/v1",
        "/api/v1",
        "/admin/state",
        "/_admin/state",
        "/_twin/state",
        "/inspect",
        "/reset",
        "/api/admin/state",
        "/openapi.json",
        "/docs",
        "/swagger",
        "/redoc",
        "/schema",
        "/health",
        "/healthz",
        "/ready",
        "/metrics",
        "/.well-known/openid-configuration",
        "//evil.example/path",
        "/%2F%2Fevil.example/path",
        "/safe/../admin/state",
        "/safe/%2e%2e/admin/state",
        "https://slack.com/api/conversations.list",
        "api/conversations.list",
        "/api/conversations.list#frag",
    ],
)
async def test_blocked_paths_are_refused_before_any_network(path: str) -> None:
    recorder = Recorder()
    gateway = make_gateway(recorder)
    result = await gateway.execute_tool("provider_api", {"provider": "slack", "method": "GET", "path": path})
    assert tuple(result) == ENVELOPE_KEYS
    assert result["ok"] is False
    assert result["status_code"] is None and result["body"] is None and result["headers"] == {}
    assert result["error"]
    assert recorder.requests == []
    trace = result["trace"]
    assert tuple(trace) == TRACE_KEYS
    assert trace["sequence"] == 1 and trace["error"] == result["error"]
    assert trace["provider"] == "slack" and trace["request_fingerprint"] is None
    assert HEX64.match(trace["attempt_fingerprint"])
    assert len(gateway.trace) == 1
    await gateway.aclose()


async def test_graphql_introspection_is_blocked_in_body_and_query() -> None:
    recorder = Recorder()
    linear = RealAppConfig.for_provider("linear", base_url="http://127.0.0.1:8090", token="lin_api_unit")
    gateway = RealAppGateway([linear], env={}, transport=recorder.transport)
    body_result = await gateway.execute_tool(
        "provider_api",
        {
            "provider": "linear",
            "method": "POST",
            "path": "/graphql",
            "body": {"query": "{ __schema { types { name } } }"},
        },
    )
    query_result = await gateway.execute_tool(
        "provider_api",
        {
            "provider": "linear",
            "method": "GET",
            "path": "/graphql",
            "query": {"query": 'query { __type(name: "Issue") { name } }'},
        },
    )
    allowed = await gateway.execute_tool(
        "provider_api",
        {
            "provider": "linear",
            "method": "POST",
            "path": "/graphql",
            "body": {"query": "query Issues { issues { nodes { id } } }"},
        },
    )
    assert body_result["ok"] is False and "introspection" in body_result["error"]
    assert query_result["ok"] is False and "introspection" in query_result["error"]
    assert allowed["ok"] is True
    assert allowed["trace"]["operation"] == "issues" and allowed["trace"]["operation_type"] == "query"
    assert len(recorder.requests) == 1
    await gateway.aclose()


async def test_role_alias_resolves_to_the_provider_request() -> None:
    recorder = Recorder()
    gateway = make_gateway(recorder, slack_config(), stripe_config())
    result = await gateway.execute_tool(
        "provider_api", {"provider": "team_chat", "method": "GET", "path": "/api/users.list"}
    )
    payments = await gateway.execute_tool(
        "provider_api", {"provider": "payments", "method": "GET", "path": "/v1/customers"}
    )
    assert result["ok"] is True
    assert result["requested_provider"] == "team_chat" and result["provider"] == "slack"
    assert result["trace"]["requested_provider"] == "team_chat" and result["trace"]["provider"] == "slack"
    assert recorder.requests[0].url.host == "slack.com"
    assert payments["provider"] == "stripe" and recorder.requests[1].url.host == "api.stripe.com"
    assert gateway.roles() == {"team_chat": "slack", "payments": "stripe"}
    await gateway.aclose()


async def test_unknown_provider_is_refused_without_a_request() -> None:
    recorder = Recorder()
    gateway = make_gateway(recorder)
    result = await gateway.execute_tool(
        "provider_api", {"provider": "hubspot", "method": "GET", "path": "/crm/v3/objects/companies"}
    )
    assert result["ok"] is False and result["provider"] is None
    assert (
        "unknown provider 'hubspot'" in result["error"]
        and "slack" in result["error"]
        and "team_chat" in result["error"]
    )
    assert result["trace"]["provider"] is None
    assert recorder.requests == []
    await gateway.aclose()


async def test_unknown_tool_name_is_an_error_envelope() -> None:
    gateway = make_gateway(Recorder())
    result = await gateway.execute_tool("shell", {})
    assert result["ok"] is False and "unknown tool" in result["error"]
    assert gateway.calls == 0
    await gateway.aclose()


# -- bodies -----------------------------------------------------------------------------------


async def test_stripe_defaults_to_form_encoding_with_bracket_notation() -> None:
    recorder = Recorder(lambda _request: httpx.Response(200, json={"id": "cus_1"}))
    gateway = make_gateway(recorder, stripe_config())
    result = await gateway.execute_tool(
        "provider_api",
        {
            "provider": "stripe",
            "method": "POST",
            "path": "/v1/customers/cus_1",
            "headers": {"Idempotency-Key": "bp-unit-1"},
            "body": {"name": "Rivermill Studio", "metadata": {"request": "bp-1"}, "expand": ["subscriptions", "tax"]},
        },
    )
    assert result["ok"] is True
    request = recorder.requests[0]
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert request.headers["idempotency-key"] == "bp-unit-1"
    assert request.content.decode() == (
        "expand%5B%5D=subscriptions&expand%5B%5D=tax&metadata%5Brequest%5D=bp-1&name=Rivermill+Studio"
    )
    await gateway.aclose()


async def test_slack_bodies_are_json_with_utf8_charset() -> None:
    recorder = Recorder(lambda _request: httpx.Response(200, json={"ok": True, "ts": "1.000001"}))
    gateway = make_gateway(recorder)
    body = {"channel": "C0UNIT", "text": "Review request: Rivermill Studio → ap@rivermill.example"}
    result = await gateway.execute_tool(
        "provider_api", {"provider": "slack", "method": "POST", "path": "/api/chat.postMessage", "body": body}
    )
    assert result["ok"] is True
    request = recorder.requests[0]
    assert request.headers["content-type"] == "application/json; charset=utf-8"
    assert request.content == json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    await gateway.aclose()


async def test_body_encoding_can_be_overridden_and_is_validated() -> None:
    recorder = Recorder()
    gateway = make_gateway(recorder, stripe_config())
    as_json = await gateway.execute_tool(
        "provider_api",
        {
            "provider": "stripe",
            "method": "POST",
            "path": "/v1/customers",
            "body": {"name": "x"},
            "body_encoding": "json",
        },
    )
    bad = await gateway.execute_tool(
        "provider_api",
        {
            "provider": "stripe",
            "method": "POST",
            "path": "/v1/customers",
            "body": {"name": "x"},
            "body_encoding": "xml",
        },
    )
    non_object_form = await gateway.execute_tool(
        "provider_api", {"provider": "stripe", "method": "POST", "path": "/v1/customers", "body": ["a", "b"]}
    )
    assert as_json["ok"] is True
    assert recorder.requests[0].headers["content-type"] == "application/json; charset=utf-8"
    assert bad["ok"] is False and "body_encoding" in bad["error"]
    assert non_object_form["ok"] is False and "form-encoded body must be an object" in non_object_form["error"]
    assert len(recorder.requests) == 1
    await gateway.aclose()


async def test_oversized_request_bodies_are_refused() -> None:
    recorder = Recorder()
    gateway = make_gateway(recorder, request_limit_bytes=64)
    result = await gateway.execute_tool(
        "provider_api",
        {"provider": "slack", "method": "POST", "path": "/api/chat.postMessage", "body": {"text": "x" * 100}},
    )
    assert result["ok"] is False and "exceeds the 64-byte gateway limit" in result["error"]
    assert recorder.requests == []
    await gateway.aclose()


# -- query handling and fingerprints ----------------------------------------------------------


async def test_query_pairs_merge_sorted_and_fingerprints_are_stable() -> None:
    recorder = Recorder()
    gateway = make_gateway(recorder)
    first = await gateway.execute_tool(
        "provider_api",
        {
            "provider": "slack",
            "method": "GET",
            "path": "/api/conversations.history?b=2",
            "query": {"c": [True, "x"], "a": 1},
        },
    )
    second = await gateway.execute_tool(
        "provider_api",
        {
            "provider": "slack",
            "method": "GET",
            "path": "/api/conversations.history?b=2",
            "query": {"a": 1, "c": [True, "x"]},
        },
    )
    third = await gateway.execute_tool(
        "provider_api",
        {
            "provider": "slack",
            "method": "GET",
            "path": "/api/conversations.history?b=3",
            "query": {"a": 1, "c": [True, "x"]},
        },
    )
    assert recorder.requests[0].url.query == b"a=1&b=2&c=true&c=x"
    assert first["path"] == "/api/conversations.history?a=1&b=2&c=true&c=x"
    assert first["trace"]["request_fingerprint"] == second["trace"]["request_fingerprint"]
    assert first["trace"]["action_fingerprint"] == second["trace"]["action_fingerprint"]
    assert first["trace"]["request_fingerprint"] != third["trace"]["request_fingerprint"]
    assert first["trace"]["sequence"], second["trace"]["sequence"] == (1, 2)
    await gateway.aclose()


def test_action_fingerprint_ignores_idempotency_headers_only() -> None:
    base: dict[str, Any] = {
        "provider": "stripe",
        "method": "POST",
        "path": "/v1/customers/cus_1",
        "body": {"name": "x"},
    }
    plain = request_fingerprints(
        base, provider="stripe", effective_path="/v1/customers/cus_1", default_body_encoding="form"
    )
    keyed = request_fingerprints(
        {**base, "headers": {"Idempotency-Key": "k-1"}},
        provider="stripe",
        effective_path="/v1/customers/cus_1",
        default_body_encoding="form",
    )
    custom = request_fingerprints(
        {**base, "headers": {"X-Trace": "t-1"}},
        provider="stripe",
        effective_path="/v1/customers/cus_1",
        default_body_encoding="form",
    )
    assert plain[0] != keyed[0] and plain[1] == keyed[1]
    assert plain[0] != custom[0] and plain[1] != custom[1]
    assert all(HEX64.match(value) for value in (*plain, *keyed, *custom))


# -- custom headers ------------------------------------------------------------------------------


async def test_custom_headers_pass_through_except_blocked_ones() -> None:
    recorder = Recorder()
    gateway = make_gateway(recorder)
    allowed = await gateway.execute_tool(
        "provider_api",
        {"provider": "slack", "method": "GET", "path": "/api/users.list", "headers": {"If-None-Match": "etag-1"}},
    )
    assert allowed["ok"] is True and recorder.requests[0].headers["if-none-match"] == "etag-1"
    for blocked in (
        {"Authorization": "Bearer stolen"},
        {"Cookie": "a=b"},
        {"X-Arga-Run": "1"},
        {"Host": "evil"},
        {"Content-Type": "text/plain"},
    ):
        result = await gateway.execute_tool(
            "provider_api", {"provider": "slack", "method": "GET", "path": "/api/users.list", "headers": blocked}
        )
        assert result["ok"] is False and "cannot be supplied by the candidate" in result["error"]
    assert len(recorder.requests) == 1
    await gateway.aclose()


# -- retries, limits, transport --------------------------------------------------------------------


async def test_rate_limits_are_retried_honouring_retry_after_with_one_trace_record() -> None:
    attempts = 0

    def responder(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(429, headers={"Retry-After": "1.5"}, json={"ok": False, "error": "ratelimited"})
        return httpx.Response(200, json={"ok": True})

    recorder = Recorder(responder)
    sleep = FakeSleep()
    gateway = make_gateway(recorder, sleep=sleep)
    result = await gateway.execute_tool(
        "provider_api", {"provider": "slack", "method": "POST", "path": "/api/chat.postMessage", "body": {"text": "x"}}
    )
    assert result["ok"] is True and result["status_code"] == 200
    assert len(recorder.requests) == 3
    assert sleep.calls == [1.5, 1.5]
    assert len(gateway.trace) == 1 and gateway.trace[0]["status_code"] == 200
    assert gateway.retries == 2
    await gateway.aclose()


async def test_retry_exhaustion_returns_the_last_response_with_default_backoff() -> None:
    recorder = Recorder(lambda _request: httpx.Response(503, text="unavailable"))
    sleep = FakeSleep()
    gateway = make_gateway(recorder, sleep=sleep)
    result = await gateway.execute_tool(
        "provider_api", {"provider": "slack", "method": "GET", "path": "/api/users.list"}
    )
    assert result["ok"] is False and result["status_code"] == 503 and result["body"] == "unavailable"
    assert len(recorder.requests) == 3
    assert sleep.calls == [2.0, 4.0]
    assert len(gateway.trace) == 1
    await gateway.aclose()


async def test_retry_after_is_capped_at_thirty_seconds() -> None:
    recorder = Recorder(lambda _request: httpx.Response(429, headers={"Retry-After": "120"}))
    sleep = FakeSleep()
    gateway = make_gateway(recorder, sleep=sleep, max_attempts=2)
    await gateway.execute_tool("provider_api", {"provider": "slack", "method": "GET", "path": "/api/users.list"})
    assert sleep.calls == [30.0]
    await gateway.aclose()


async def test_call_limit_counts_every_attempt_including_blocked_ones() -> None:
    recorder = Recorder()
    gateway = make_gateway(recorder, max_calls=2)
    await gateway.execute_tool("provider_api", {"provider": "slack", "method": "GET", "path": "/admin/state"})
    await gateway.execute_tool("provider_api", {"provider": "slack", "method": "GET", "path": "/api/users.list"})
    third = await gateway.execute_tool(
        "provider_api", {"provider": "slack", "method": "GET", "path": "/api/users.list"}
    )
    assert third["ok"] is False
    assert third["error"] == "provider_api call limit of 2 has been reached"
    assert len(recorder.requests) == 1
    assert len(gateway.trace) == 3 and gateway.trace[2]["error"] == third["error"]
    await gateway.aclose()


async def test_transport_errors_are_envelopes_not_exceptions() -> None:
    def responder(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    recorder = Recorder(responder)
    gateway = make_gateway(recorder)
    result = await gateway.execute_tool(
        "provider_api", {"provider": "slack", "method": "GET", "path": "/api/users.list"}
    )
    assert result["ok"] is False and result["error"] == "transport:ConnectError"
    assert result["status_code"] is None and result["body"] is None
    assert result["trace"]["error"] == "transport:ConnectError" and result["trace"]["method"] == "GET"
    assert result["trace"]["request_fingerprint"] is not None
    await gateway.aclose()


async def test_large_bodies_are_truncated_and_flagged() -> None:
    recorder = Recorder(lambda _request: httpx.Response(200, text="y" * 100, headers={"content-type": "text/plain"}))
    gateway = make_gateway(recorder, response_limit_bytes=16)
    result = await gateway.execute_tool(
        "provider_api", {"provider": "slack", "method": "GET", "path": "/api/users.list"}
    )
    assert result["truncated"] is True and result["body"] == "y" * 16
    assert result["trace"]["truncated"] is True and result["trace"]["response_bytes"] == 16
    await gateway.aclose()


async def test_binary_bodies_are_base64_and_empty_bodies_are_null() -> None:
    responses = iter(
        [
            httpx.Response(200, content=b"\x89PNG\r\n", headers={"content-type": "image/png"}),
            httpx.Response(204),
        ]
    )
    recorder = Recorder(lambda _request: next(responses))
    gateway = make_gateway(recorder)
    binary = await gateway.execute_tool(
        "provider_api", {"provider": "slack", "method": "GET", "path": "/api/files.info"}
    )
    empty = await gateway.execute_tool(
        "provider_api", {"provider": "slack", "method": "GET", "path": "/api/files.info"}
    )
    assert binary["body"] == {"encoding": "base64", "data": "iVBORw0K"}
    assert empty["ok"] is True and empty["body"] is None
    await gateway.aclose()


async def test_delete_reaches_the_provider_like_the_harness_gateway() -> None:
    # The gateway is not the safety layer: DELETE passes here (as on the twins) and is refused by gate.py.
    recorder = Recorder(lambda _request: httpx.Response(200, json={"id": "cus_1", "deleted": True}))
    gateway = make_gateway(recorder, stripe_config())
    result = await gateway.execute_tool(
        "provider_api", {"provider": "stripe", "method": "DELETE", "path": "/v1/customers/cus_1"}
    )
    assert result["ok"] is True and recorder.requests[0].method == "DELETE"
    await gateway.aclose()


# -- safety guards -----------------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["sk_live_0123456789abcdef", "rk_test_0123456789abcdef", "pk_test_0123456789abcdef"])
def test_stripe_host_requires_a_test_mode_secret_key(key: str) -> None:
    with pytest.raises(ScratchGuardError):
        RealAppGateway([stripe_config(key)], env=SCRATCH_ENV)


def test_stripe_host_refuses_dynamic_credentials() -> None:
    async def auth() -> dict[str, str]:
        return {"Authorization": f"Bearer {STRIPE_TEST_KEY}"}

    config = RealAppConfig(provider="stripe", role="payments", base_url="https://api.stripe.com", auth=auth)
    with pytest.raises(ScratchGuardError):
        RealAppGateway([config], env=SCRATCH_ENV)


def test_real_hosts_need_scratch_ok_but_loopback_twins_do_not() -> None:
    with pytest.raises(ScratchGuardError):
        RealAppGateway([slack_config()], env={})
    twin = RealAppGateway([slack_config(base_url="http://127.0.0.1:8081")], env={})
    assert twin.describe()["slack"]["loopback"] is True
    local = RealAppGateway([slack_config(base_url="http://localhost:8081")], env={})
    assert local.provider_names() == ("slack",)


def test_gateway_configuration_is_validated() -> None:
    with pytest.raises(ValueError):
        RealAppGateway([], env=SCRATCH_ENV)
    with pytest.raises(ValueError):
        RealAppGateway([slack_config(), slack_config()], env=SCRATCH_ENV)
    with pytest.raises(ValueError):
        RealAppConfig.for_provider("slack", base_url="https://slack.com/api", token="x")
    with pytest.raises(ValueError):
        RealAppConfig.for_provider("slack", base_url="ftp://slack.com", token="x")
    with pytest.raises(ValueError):
        RealAppConfig.for_provider("discord", base_url="https://discord.com", token="x")
    clash = RealAppConfig(provider="team_chat", role="chat", base_url="http://127.0.0.1:1")
    with pytest.raises(ValueError):
        RealAppGateway([slack_config(base_url="http://127.0.0.1:2"), clash], env={})


async def test_credentials_never_appear_in_results_traces_or_descriptions() -> None:
    recorder = Recorder(
        lambda _request: httpx.Response(200, json={"echo": f"Bearer {SLACK_TOKEN}", "url": "https://slack.com/x"})
    )
    gateway = make_gateway(recorder)
    result = await gateway.execute_tool(
        "provider_api", {"provider": "slack", "method": "GET", "path": "/api/auth.test"}
    )
    # The harness scrubs the whole credential header value as well as the bare token (gateway.py:1141-1152).
    assert result["body"] == {"echo": "[redacted]", "url": "/x"}
    for rendered in (
        json.dumps(result),
        json.dumps(gateway.trace),
        repr(gateway),
        json.dumps(gateway.describe()),
        repr(slack_config()),
    ):
        assert SLACK_TOKEN not in rendered
    assert gateway.describe()["slack"]["headers"] == {"Authorization": "Bearer <redacted>"}
    assert gateway.describe()["slack"]["role"] == "team_chat"
    await gateway.aclose()


async def test_sensitive_response_headers_are_dropped() -> None:
    recorder = Recorder(
        lambda _request: httpx.Response(200, json={}, headers={"Set-Cookie": "sid=1", "X-Twin-Id": "t", "ETag": "e1"})
    )
    gateway = make_gateway(recorder)
    result = await gateway.execute_tool(
        "provider_api", {"provider": "slack", "method": "GET", "path": "/api/auth.test"}
    )
    assert "set-cookie" not in result["headers"] and "x-twin-id" not in result["headers"]
    assert result["headers"]["etag"] == "e1"
    await gateway.aclose()


# -- provider_docs ------------------------------------------------------------------------------------


async def test_provider_docs_is_unavailable_identically_without_an_executor() -> None:
    gateway = make_gateway(Recorder())
    result = await gateway.execute_tool(
        "provider_docs", {"provider": "team_chat", "action": "search", "query": "chat.postMessage"}
    )
    assert result["ok"] is False
    assert result["error"] == "provider_docs unavailable in this substrate"
    assert (
        result["requested_provider"] == "team_chat" and result["provider"] == "slack" and result["action"] == "search"
    )
    assert gateway.docs_calls == 1 and gateway.calls == 0
    await gateway.aclose()


async def test_provider_docs_delegates_and_enforces_its_own_limit() -> None:
    seen: list[tuple[str, dict[str, Any]]] = []

    async def docs(tool_name: str, tool_input: dict[str, Any]) -> object:
        seen.append((tool_name, tool_input))
        return {"ok": True, "documents": []}

    gateway = make_gateway(Recorder(), docs_executor=docs, max_docs_calls=1)
    first = await gateway.execute_tool("provider_docs", {"provider": "slack", "action": "search"})
    second = await gateway.execute_tool("provider_docs", {"provider": "slack", "action": "search"})
    assert first == {"ok": True, "documents": []}
    assert seen == [("provider_docs", {"provider": "slack", "action": "search"})]
    assert second["ok"] is False and second["error"] == "provider_docs call limit of 1 has been reached"
    await gateway.aclose()


# -- schema ------------------------------------------------------------------------------------------


def test_tool_schema_mirrors_the_harness_with_names_and_roles() -> None:
    gateway = RealAppGateway([slack_config(), stripe_config()], env=SCRATCH_ENV)
    schema = gateway.tool_schema()
    assert [tool["name"] for tool in schema] == ["provider_api", "provider_docs"]
    api, docs = schema
    assert api["input_schema"]["properties"]["provider"]["enum"] == ["payments", "slack", "stripe", "team_chat"]
    assert docs["input_schema"]["properties"]["provider"]["enum"] == ["payments", "slack", "stripe", "team_chat"]
    assert api["input_schema"]["required"] == ["provider", "method", "path"]
    assert api["input_schema"]["properties"]["method"]["enum"] == ["DELETE", "GET", "PATCH", "POST", "PUT"]
    assert api["input_schema"]["properties"]["body_encoding"]["enum"] == ["json", "form"]
    assert api["input_schema"]["additionalProperties"] is False
    assert docs["input_schema"]["required"] == ["provider", "action"]
    assert docs["input_schema"]["properties"]["action"]["enum"] == ["search", "fetch"]
    schema[0]["name"] = "mutated"
    assert gateway.tool_schema()[0]["name"] == "provider_api"


# -- construction from the environment -----------------------------------------------------------------


def test_gateway_from_env_prefers_devsim_twins_with_dummy_tokens() -> None:
    env = {
        "DEVSIM_SLACK_URL": "http://127.0.0.1:8081",
        "SLACK_BOT_TOKEN": SLACK_TOKEN,
        "STRIPE_SECRET_KEY": STRIPE_TEST_KEY,
        **SCRATCH_ENV,
    }
    gateway = gateway_from_env(["slack", "stripe"], env=env)
    described = gateway.describe()
    assert described["slack"]["base_url"] == "http://127.0.0.1:8081" and described["slack"]["loopback"] is True
    assert described["stripe"]["base_url"] == "https://api.stripe.com"
    assert (
        described["stripe"]["default_body_encoding"] == "form" and described["slack"]["default_body_encoding"] == "json"
    )
    twin = config_from_env("slack", env)
    assert twin.headers["Authorization"].startswith("Bearer xoxb-") and SLACK_TOKEN not in twin.headers["Authorization"]
    github = config_from_env("github", {"GITHUB_TOKEN": "ghp_unit"})
    assert github.headers == {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": "Bearer ghp_unit",
    }
    linear = config_from_env("linear", {"LINEAR_API_KEY": "lin_api_unit"})
    assert linear.headers == {"Authorization": "lin_api_unit"} and linear.role == "linear_tracker"


def test_gateway_from_env_names_the_missing_variable() -> None:
    with pytest.raises(ValueError, match="SLACK_BOT_TOKEN"):
        gateway_from_env(["slack"], env=SCRATCH_ENV)
    with pytest.raises(ValueError, match="unknown provider"):
        gateway_from_env(["discord"], env=SCRATCH_ENV)
    with pytest.raises(ValueError, match="DEVSIM_JIRA_URL"):
        gateway_from_env(["jira"], env=SCRATCH_ENV)
    with pytest.raises(ScratchGuardError):
        gateway_from_env(["stripe"], env={"STRIPE_SECRET_KEY": "sk_live_x", **SCRATCH_ENV})


# -- gmail token provider ----------------------------------------------------------------------------------


class FakeTokenEndpoint:
    def __init__(self, *, fail: bool = False) -> None:
        self.posts: list[dict[str, str]] = []
        self.fail = fail

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://oauth2.googleapis.com/token")
        form = dict(pair.split("=", 1) for pair in request.content.decode().split("&"))
        self.posts.append(form)
        if self.fail:
            return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Bad Request"})
        return httpx.Response(200, json={"access_token": f"ya29.unit-{len(self.posts)}", "expires_in": 3600})


async def test_gmail_static_token_is_used_directly() -> None:
    provider = GmailTokenProvider(
        {"GMAIL_ACCESS_TOKEN": "ya29.static"}, transport=httpx.MockTransport(FakeTokenEndpoint())
    )
    assert await provider.headers() == {"Authorization": "Bearer ya29.static"}
    assert provider.uses_refresh_flow is False
    await provider.aclose()


async def test_gmail_refresh_flow_caches_until_shortly_before_expiry() -> None:
    endpoint = FakeTokenEndpoint()
    clock = {"now": 1000.0}
    env = {"GMAIL_CLIENT_ID": "cid", "GMAIL_CLIENT_SECRET": "csecret", "GMAIL_REFRESH_TOKEN": "rt-1"}
    headers = await gmail_token_provider(env, transport=httpx.MockTransport(endpoint), now=lambda: clock["now"])
    assert await headers() == {"Authorization": "Bearer ya29.unit-1"}
    assert await headers() == {"Authorization": "Bearer ya29.unit-1"}
    assert len(endpoint.posts) == 1
    assert endpoint.posts[0] == {
        "grant_type": "refresh_token",
        "client_id": "cid",
        "client_secret": "csecret",
        "refresh_token": "rt-1",
    }
    clock["now"] = 1000.0 + 3600.0 - 61.0
    assert await headers() == {"Authorization": "Bearer ya29.unit-1"}
    clock["now"] = 1000.0 + 3600.0 - 59.0
    assert await headers() == {"Authorization": "Bearer ya29.unit-2"}
    assert len(endpoint.posts) == 2


def test_gmail_provider_lists_missing_variables() -> None:
    with pytest.raises(ValueError, match="GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN"):
        GmailTokenProvider({"GMAIL_CLIENT_ID": "cid"})


async def test_gmail_refresh_failure_is_an_auth_error_and_an_envelope() -> None:
    endpoint = FakeTokenEndpoint(fail=True)
    env = {"GMAIL_CLIENT_ID": "cid", "GMAIL_CLIENT_SECRET": "csecret", "GMAIL_REFRESH_TOKEN": "rt-1"}
    with pytest.raises(AuthError, match="invalid_grant"):
        await gmail_token_provider(env, transport=httpx.MockTransport(endpoint))
    provider = GmailTokenProvider(env, transport=httpx.MockTransport(endpoint))
    config = RealAppConfig.for_provider("gmail", base_url="https://gmail.googleapis.com", auth=provider.headers)
    recorder = Recorder()
    gateway = RealAppGateway([config], env=SCRATCH_ENV, transport=recorder.transport)
    result = await gateway.execute_tool(
        "provider_api", {"provider": "email", "method": "GET", "path": "/gmail/v1/users/me/labels"}
    )
    assert result["ok"] is False and result["error"].startswith("auth:")
    assert result["trace"]["error"] == result["error"] and recorder.requests == []
    await gateway.aclose()
    await provider.aclose()


async def test_dynamic_credentials_are_sent_and_redacted() -> None:
    secret = "ya29.dynamic-secret-token-0123"

    async def auth() -> dict[str, str]:
        return {"Authorization": f"Bearer {secret}"}

    config = RealAppConfig.for_provider("gmail", base_url="https://gmail.googleapis.com", auth=auth)
    recorder = Recorder(lambda request: httpx.Response(200, json={"echo": request.headers["authorization"]}))
    gateway = RealAppGateway([config], env=SCRATCH_ENV, transport=recorder.transport)
    result = await gateway.execute_tool(
        "provider_api", {"provider": "gmail", "method": "GET", "path": "/gmail/v1/users/me/profile"}
    )
    assert recorder.requests[0].headers["authorization"] == f"Bearer {secret}"
    assert result["body"] == {"echo": "[redacted]"}
    assert secret not in json.dumps(gateway.describe())
    await gateway.aclose()
