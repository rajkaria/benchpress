"""The gateway's MCP tools: the same service, the same receipts, over in-memory and streamable HTTP transports."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import anyio
import httpx
import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, TextContent

import benchpress
from benchpress.gateway.app import create_app
from benchpress.gateway.auth import RateLimiter
from benchpress.gateway.config import Settings
from benchpress.gateway.deps import header_authenticator
from benchpress.gateway.mcp_server import RULE_HELP, build_mcp_server
from benchpress.gateway.store import Store, WorkspaceRow
from tests.gateway.test_app import FIXED, PROMPT, WRITE
from tests.test_verified import FakeProvider


def _service(tmp_path: Path, **settings: Any) -> tuple[Any, WorkspaceRow, FakeProvider, str]:
    url = f"sqlite:///{tmp_path / 'mcp.db'}"
    store = Store.open(url)
    ws = store.create_workspace("acme")
    _, key = store.create_api_key(ws.id, "mcp")
    provider = FakeProvider()
    app = create_app(Settings(store=url, **settings), store=store, executor=provider.execute_tool, clock=lambda: FIXED)
    return app, ws, provider, key


def _text(result: CallToolResult) -> str:
    return "\n".join(block.text for block in result.content if isinstance(block, TextContent))


async def test_tools_are_listed_and_verified_write_matches_http(tmp_path: Path) -> None:
    app, ws, provider, _ = _service(tmp_path)

    async def authenticate(header: str | None) -> WorkspaceRow:
        return ws

    server = build_mcp_server(app.state.service, authenticate)
    async with Client(server) as client:
        names = sorted(tool.name for tool in (await client.list_tools()).tools)
        assert names == ["explain_refusal", "read", "verified_write"]
        result = await client.call_tool("verified_write", {"action": WRITE, "context": {"user_prompt": PROMPT}})
        assert not result.is_error
        payload: dict[str, Any] = result.structured_content or {}
        assert payload["status"] == "verified" and payload["receipt_id"]
        assert [c["method"] for c in provider.calls] == ["PATCH", "GET"]
        refused = await client.call_tool(
            "explain_refusal",
            {"action": {**WRITE, "path": "/crm/v3/objects/companies/702"},
             "context": {"user_prompt": PROMPT, "protected": {"ids": ["702"]}}},
        )
        explained: dict[str, Any] = refused.structured_content or {}
        assert explained["rule"] == "protected" and explained["help"] == RULE_HELP["protected"]
        bad = await client.call_tool("verified_write", {"action": {"id": "x"}, "context": {"user_prompt": PROMPT}})
        assert bad.is_error
    assert len(provider.calls) == 2, "explain_refusal never executes"


def test_every_gate_rule_has_help() -> None:
    for rule in ("control_plane", "method", "action_class", "protected", "provider_scope", "plan_membership",
                 "field_smuggling", "external_destination", "idempotency", "policy_pack", "allowed"):
        assert RULE_HELP[rule].strip()


async def test_explain_refusal_helps_with_allowed_writes_and_pack_rules(tmp_path: Path) -> None:
    app, ws, provider, _ = _service(tmp_path)
    billing = (Path(benchpress.__file__).parent / "policy_packs" / "billing.yaml").read_text(encoding="utf-8")
    app.state.store.put_policy(ws.id, "billing", billing)

    async def authenticate(header: str | None) -> WorkspaceRow:
        return ws

    void: dict[str, Any] = {
        "id": "v1", "kind": "update", "provider": "stripe", "method": "POST", "path": "/v1/invoices/in_1/void",
        "body": {}, "fields": [], "satisfies": ["end_state[0]"],
    }
    async with Client(build_mcp_server(app.state.service, authenticate)) as client:
        allowed = await client.call_tool("explain_refusal", {"action": WRITE, "context": {"user_prompt": PROMPT}})
        assert allowed.structured_content == {
            "allowed": True, "rule": "allowed", "reason": "", "help": RULE_HELP["allowed"]
        }
        pack = await client.call_tool("explain_refusal", {"action": void, "context": {"user_prompt": PROMPT}})
        explained: dict[str, Any] = pack.structured_content or {}
        assert explained["allowed"] is False and str(explained["rule"]).startswith("pack:billing.")
        assert explained["help"] == RULE_HELP["policy_pack"]
    assert provider.calls == []


async def test_tool_errors_carry_the_message_never_a_secret_and_never_stop_the_server(tmp_path: Path) -> None:
    app, ws, provider, _ = _service(tmp_path)
    secret = "sk_live_rivermill_0123456789abcdef"

    async def authenticate(header: str | None) -> WorkspaceRow:
        return ws

    async with Client(build_mcp_server(app.state.service, authenticate)) as client:
        leaky = {**WRITE, "headers": {"Authorization": f"Bearer {secret}"}}
        leaked = await client.call_tool("verified_write", {"action": leaky, "context": {"user_prompt": PROMPT}})
        assert leaked.is_error and "credentials belong in upstream config" in _text(leaked)
        assert secret not in _text(leaked)

        both = await client.call_tool("explain_refusal", {"action": WRITE, "session_id": "s1",
                                                          "context": {"user_prompt": PROMPT}})
        assert both.is_error and "exactly one of session_id or context" in _text(both)

        missing = await client.call_tool("verified_write", {"action": WRITE, "session_id": "nope"})
        assert missing.is_error and "no session 'nope'" in _text(missing)

        blocked = await client.call_tool("read", {"provider": "hubspot", "path": "/admin/users"})
        assert blocked.is_error and "reads never reach the control plane" in _text(blocked)

        read = await client.call_tool("read", {"provider": "hubspot", "path": "/crm/v3/objects/companies/701"})
        assert not read.is_error
        assert read.structured_content == {
            "ok": True, "status_code": 200, "body": {"id": "701", "email": "old@rivermill.example"}, "error": None
        }
    assert [c["method"] for c in provider.calls] == ["GET"]


async def test_a_refused_authentication_is_a_tool_error(tmp_path: Path) -> None:
    app, _, provider, _ = _service(tmp_path)

    async def authenticate(header: str | None) -> WorkspaceRow:
        raise PermissionError("a valid API key is required")

    async with Client(build_mcp_server(app.state.service, authenticate)) as client:
        for name, arguments in (
            ("verified_write", {"action": WRITE, "context": {"user_prompt": PROMPT}}),
            ("read", {"provider": "hubspot", "path": "/crm/v3/objects/companies/701"}),
            ("explain_refusal", {"action": WRITE, "context": {"user_prompt": PROMPT}}),
        ):
            denied = await client.call_tool(name, arguments)
            assert denied.is_error and "a valid API key is required" in _text(denied), name
    assert provider.calls == []


async def test_header_authenticator_applies_the_http_rules(tmp_path: Path) -> None:
    app, ws, _, key = _service(tmp_path, requests_per_minute=1)
    authenticate = header_authenticator(app.state.service, app.state.limiter)
    assert (await authenticate(f"Bearer {key}")).id == ws.id
    for header, message in (
        (f"Bearer {key}", "rate limit exceeded"),
        (None, "a valid API key is required"),
        ("Bearer bp_unknown", "a valid API key is required"),
        (key, "a valid API key is required"),
    ):
        with pytest.raises(PermissionError) as refused:
            await authenticate(header)
        assert str(refused.value) == message

    (tmp_path / "open").mkdir()
    open_app, _, _, _ = _service(tmp_path / "open", auth="none")
    anonymous = header_authenticator(open_app.state.service, RateLimiter(1))
    assert (await anonymous(None)).name == "default"
    assert (await anonymous("Bearer bp_unknown")).name == "default"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def _running(app: Any) -> Generator[str]:
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if not thread.is_alive():
            raise RuntimeError("uvicorn exited before it started")
        if time.monotonic() > deadline:
            server.should_exit = True
            raise TimeoutError("uvicorn did not start")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive(), "uvicorn did not shut down"


async def test_streamable_http_mcp_requires_a_key(tmp_path: Path) -> None:
    app, _, provider, key = _service(tmp_path)
    with _running(app) as base, anyio.fail_after(30):
        async with (
            httpx2.AsyncClient(headers={"Authorization": f"Bearer {key}"}) as http,
            Client(streamable_http_client(f"{base}/mcp/", http_client=http)) as client,
        ):
            result = await client.call_tool("verified_write", {"action": WRITE, "context": {"user_prompt": PROMPT}})
            assert (result.structured_content or {})["status"] == "verified"
        async with Client(f"{base}/mcp/") as anonymous:
            denied = await anonymous.call_tool("verified_write", {"action": WRITE, "context": {"user_prompt": PROMPT}})
            assert denied.is_error and "a valid API key is required" in _text(denied)
    assert [c["method"] for c in provider.calls] == ["PATCH", "GET"]
    metrics = app.state.metrics.render()[0].decode()
    assert 'route="/mcp"' in metrics and 'route="unmatched"' not in metrics


async def test_streamable_http_mcp_shares_the_http_rate_limit_and_receipts(tmp_path: Path) -> None:
    app, ws, provider, key = _service(tmp_path, requests_per_minute=2)
    headers = {"Authorization": f"Bearer {key}"}
    with _running(app) as base, anyio.fail_after(30):
        async with (
            httpx2.AsyncClient(headers=headers) as http,
            Client(streamable_http_client(f"{base}/mcp/", http_client=http)) as client,
        ):
            result = await client.call_tool("verified_write", {"action": WRITE, "context": {"user_prompt": PROMPT}})
            written: dict[str, Any] = result.structured_content or {}
            receipt_id = str(written["receipt_id"])
            async with httpx.AsyncClient(base_url=base, headers=headers) as rest:
                receipt = await rest.get(f"/v1/receipts/{receipt_id}")
                assert receipt.status_code == 200 and receipt.json()["payload"]["workspace"] == ws.name
                limited = await rest.get("/v1/receipts")
                assert limited.status_code == 429
            over = await client.call_tool("read", {"provider": "hubspot", "path": "/crm/v3/objects/companies/701"})
            assert over.is_error and "rate limit exceeded" in _text(over)
    assert [c["method"] for c in provider.calls] == ["PATCH", "GET"]
