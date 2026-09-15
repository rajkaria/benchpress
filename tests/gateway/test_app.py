"""The gateway over HTTP: sessions, execute, receipts, policies, auth, limits, cross-replica idempotency."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MethodType
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from benchpress.context import Action
from benchpress.gateway.app import create_app
from benchpress.gateway.config import ConfigError, Settings, UpstreamConfig
from benchpress.gateway.executors import executor_from_settings
from benchpress.gateway.schemas import ContextInput, ProtectedInput, SessionCreate
from benchpress.gateway.service import GatewayService, RequestRejected
from benchpress.gateway.store import Store
from benchpress.realapp import DEFAULT_MAX_CALLS, RealAppGateway
from benchpress.tools import PROVIDER_API, as_mapping
from tests.test_verified import FakeProvider

FIXED = "2026-09-14T00:00:00.000Z"
PROMPT = "Rivermill Studio asked for renewal notices to go to ap@rivermill.example."
WRITE = {
    "id": "w1",
    "kind": "update",
    "provider": "hubspot",
    "method": "PATCH",
    "path": "/crm/v3/objects/companies/701",
    "body": {"properties": {"email": "ap@rivermill.example"}},
    "fields": ["email"],
    "satisfies": ["end_state[0]"],
    "readback": {"path": "/crm/v3/objects/companies/701", "field_path": "email"},
}


@dataclass
class Gateway:
    client: TestClient
    store: Store
    provider: FakeProvider
    key: str

    def headers(self, key: str | None = None) -> dict[str, str]:
        return {"Authorization": f"Bearer {key or self.key}"}


@contextmanager
def _gateway(url: str, **settings: Any) -> Generator[Gateway]:
    store = Store.open(url)
    ws = store.workspace_by_name("acme") or store.create_workspace("acme")
    _, key = store.create_api_key(ws.id, "test")
    provider = FakeProvider()
    app = create_app(Settings(store=url, **settings), store=store, executor=provider.execute_tool, clock=lambda: FIXED)
    try:
        with TestClient(app) as client:
            yield Gateway(client, store, provider, key)
    finally:
        store.engine.dispose()


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'gw.db'}"


@pytest.fixture
def gw(db_url: str) -> Iterator[Gateway]:
    with _gateway(db_url) as gateway:
        yield gateway


def _execute(gw: Gateway, session_id: str, action: dict[str, Any]) -> dict[str, Any]:
    response = gw.client.post("/v1/execute", json={"session_id": session_id, "action": action}, headers=gw.headers())
    return response.json()


def _session(gw: Gateway, session_id: str = "s1", **context: Any) -> None:
    body = {"session_id": session_id, "context": {"user_prompt": PROMPT, **context}}
    response = gw.client.post("/v1/sessions", json=body, headers=gw.headers())
    assert response.status_code == 201, response.text


def test_healthz_needs_no_key(gw: Gateway) -> None:
    body = gw.client.get("/healthz").json()
    assert body["status"] == "ok" and body["store"] == "ok"


def test_healthz_is_503_when_the_store_is_unreachable(gw: Gateway, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gw.store, "ping", lambda: False)
    response = gw.client.get("/healthz")
    assert response.status_code == 503 and response.json()["store"] == "error"


def test_missing_or_unknown_key_is_401(gw: Gateway) -> None:
    assert gw.client.post("/v1/execute", json={}).status_code == 401
    response = gw.client.post("/v1/execute", json={}, headers=gw.headers("bp_" + "x" * 40))
    assert response.status_code == 401 and response.headers["www-authenticate"] == "Bearer"


def test_execute_verifies_and_receipts_the_write(gw: Gateway) -> None:
    _session(gw)
    response = gw.client.post("/v1/execute", json={"session_id": "s1", "action": WRITE}, headers=gw.headers())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "verified" and body["status_code"] == 200 and body["receipt_id"]
    assert [c["method"] for c in gw.provider.calls] == ["PATCH", "GET"]
    page = gw.client.get("/v1/receipts", headers=gw.headers()).json()
    assert [(r["id"], r["kind"], r["status"], r["provider"]) for r in page["receipts"]] == [
        (body["receipt_id"], "write", "verified", "hubspot")
    ]
    detail = gw.client.get(f"/v1/receipts/{body['receipt_id']}", headers=gw.headers()).json()
    assert detail["kind"] == "write" and detail["html"] is False
    assert detail["payload"]["at"] == FIXED and detail["payload"]["session"] == "s1"
    assert detail["payload"]["workspace"] == "acme"
    assert gw.client.get(f"/v1/receipts/{body['receipt_id']}/html", headers=gw.headers()).status_code == 404


def test_inline_context_creates_a_session(gw: Gateway) -> None:
    payload = {"context": {"user_prompt": PROMPT}, "action": WRITE}
    response = gw.client.post("/v1/execute", json=payload, headers=gw.headers())
    body = response.json()
    assert body["status"] == "verified" and body["session_id"]


def test_each_inline_context_request_is_a_one_shot_session(gw: Gateway) -> None:
    payload = {"context": {"user_prompt": PROMPT}, "action": WRITE}
    first = gw.client.post("/v1/execute", json=payload, headers=gw.headers()).json()
    second = gw.client.post("/v1/execute", json=payload, headers=gw.headers()).json()
    assert first["session_id"] != second["session_id"]
    assert first["status"] == "verified" and second["status"] == "verified"
    assert [c["method"] for c in gw.provider.calls] == ["PATCH", "GET", "PATCH", "GET"]


@pytest.mark.parametrize(
    "payload",
    [
        {"action": WRITE},
        {"action": WRITE, "session_id": "s1", "context": {"user_prompt": PROMPT}},
        {"action": {**WRITE, "headers": {"Authorization": "Bearer leaked"}}, "context": {"user_prompt": PROMPT}},
        {"action": {**WRITE, "method": "GET"}, "context": {"user_prompt": PROMPT}},
    ],
)
def test_malformed_requests_are_422_before_any_provider_call(gw: Gateway, payload: dict[str, Any]) -> None:
    assert gw.client.post("/v1/execute", json=payload, headers=gw.headers()).status_code == 422
    assert gw.provider.calls == []


def test_the_callers_protected_set_is_enforced(gw: Gateway) -> None:
    _session(gw, protected={"ids": ["701"]})
    body = gw.client.post("/v1/execute", json={"session_id": "s1", "action": WRITE}, headers=gw.headers()).json()
    assert body["status"] == "refused" and body["verdict"]["rule"] == "protected"
    assert gw.provider.calls == []


def test_a_second_replica_refuses_the_replay(db_url: str) -> None:
    with _gateway(db_url) as first, _gateway(db_url) as second:
        _session(first)
        payload = {"session_id": "s1", "action": WRITE}
        assert first.client.post("/v1/execute", json=payload, headers=first.headers()).json()["status"] == "verified"
        body = second.client.post("/v1/execute", json=payload, headers=second.headers()).json()
        assert body["status"] == "refused" and body["verdict"]["rule"] == "idempotency"
        assert second.provider.calls == []


def test_workspace_scoped_sessions_share_write_claims(gw: Gateway) -> None:
    for session_id in ("a", "b"):
        body = {"session_id": session_id, "context": {"user_prompt": PROMPT}, "idempotency_scope": "workspace"}
        created = gw.client.post("/v1/sessions", json=body, headers=gw.headers())
        assert created.status_code == 201
        assert created.json() == {"session_id": session_id, "idempotency_scope": "workspace"}
    _session(gw, "c")
    statuses = [
        gw.client.post("/v1/execute", json={"session_id": sid, "action": WRITE}, headers=gw.headers()).json()["status"]
        for sid in ("a", "b", "c")
    ]
    assert statuses == ["verified", "refused", "verified"]


def test_a_cached_session_keeps_no_per_write_history(gw: Gateway) -> None:
    _session(gw, protected={"ids": ["702"]})
    refused = {**WRITE, "id": "w2", "path": "/crm/v3/objects/companies/702", "readback": None}
    statuses = [_execute(gw, "s1", action)["status"] for action in (WRITE, refused, WRITE)]
    assert statuses == ["verified", "refused", "refused"]
    service: GatewayService = cast(FastAPI, gw.client.app).state.service
    acme = gw.store.workspace_by_name("acme")
    assert acme is not None
    session = asyncio.run(service.sessions.get(acme, "s1"))
    assert session is not None
    ctx = session.writer.context
    assert (ctx.gate_decisions, ctx.refusals, ctx.ledger, ctx.evidence) == ([], [], [], [])


def test_session_ids_are_generated_or_validated(gw: Gateway) -> None:
    generated = gw.client.post("/v1/sessions", json={"context": {"user_prompt": PROMPT}}, headers=gw.headers())
    assert generated.status_code == 201 and re.fullmatch(r"ses_[0-9a-f]{16}", generated.json()["session_id"])
    for bad in ("", "has space", "x" * 65):
        body = {"session_id": bad, "context": {"user_prompt": PROMPT}}
        assert gw.client.post("/v1/sessions", json=body, headers=gw.headers()).status_code == 422


def test_sessions_do_not_cross_workspaces(gw: Gateway) -> None:
    _session(gw)
    other = gw.store.create_workspace("other")
    _, other_key = gw.store.create_api_key(other.id, "o")
    response = gw.client.post("/v1/execute", json={"session_id": "s1", "action": WRITE}, headers=gw.headers(other_key))
    assert response.status_code == 404
    assert gw.client.get("/v1/receipts", headers=gw.headers(other_key)).json()["receipts"] == []


def test_duplicate_session_id_is_409(gw: Gateway) -> None:
    _session(gw)
    body = {"session_id": "s1", "context": {"user_prompt": PROMPT}}
    assert gw.client.post("/v1/sessions", json=body, headers=gw.headers()).status_code == 409


def test_rate_limit_is_per_key(db_url: str) -> None:
    with _gateway(db_url, requests_per_minute=2) as gw:
        responses = [gw.client.get("/v1/receipts", headers=gw.headers()) for _ in range(3)]
        acme = gw.store.workspace_by_name("acme")
        assert acme is not None
        _, other_key = gw.store.create_api_key(acme.id, "other")
        assert gw.client.get("/v1/receipts", headers=gw.headers(other_key)).status_code == 200
    assert [r.status_code for r in responses] == [200, 200, 429]
    assert responses[2].headers["retry-after"] == "1"


def test_oversized_bodies_are_413(db_url: str) -> None:
    with _gateway(db_url, max_body_bytes=256) as gw:
        big = {"context": {"user_prompt": "x" * 1000}, "action": WRITE}
        assert gw.client.post("/v1/execute", json=big, headers=gw.headers()).status_code == 413


def test_streamed_bodies_are_counted_against_the_cap(db_url: str) -> None:
    def chunks() -> Iterator[bytes]:
        for _ in range(10):
            yield b"x" * 64

    with _gateway(db_url, max_body_bytes=256) as gw:
        response = gw.client.put("/v1/policies/big", content=chunks(), headers=gw.headers())
    assert response.status_code == 413 and response.json() == {"detail": "request body too large"}


def test_receipt_filters(gw: Gateway) -> None:
    _session(gw, protected={"ids": ["702"]})
    gw.client.post("/v1/execute", json={"session_id": "s1", "action": WRITE}, headers=gw.headers())
    refused = {**WRITE, "id": "w2", "path": "/crm/v3/objects/companies/702", "readback": None}
    gw.client.post("/v1/execute", json={"session_id": "s1", "action": refused}, headers=gw.headers())
    rules = gw.client.get("/v1/receipts?rule=protected", headers=gw.headers()).json()["receipts"]
    assert [r["rule"] for r in rules] == ["protected"]
    statuses = gw.client.get("/v1/receipts?status=verified", headers=gw.headers()).json()["receipts"]
    assert [r["status"] for r in statuses] == ["verified"]
    newest = gw.client.get("/v1/receipts?limit=1", headers=gw.headers()).json()
    assert [r["rule"] for r in newest["receipts"]] == ["protected"] and newest["next_before"]
    older = gw.client.get(f"/v1/receipts?limit=1&before={newest['next_before']}", headers=gw.headers()).json()
    assert [r["rule"] for r in older["receipts"]] == ["allowed"]
    assert gw.client.get("/v1/receipts?limit=0", headers=gw.headers()).status_code == 422


def test_receipt_ids_that_are_not_row_ids_are_404_not_500(gw: Gateway) -> None:
    _session(gw)
    gw.client.post("/v1/execute", json={"session_id": "s1", "action": WRITE}, headers=gw.headers())
    for bad in ("abc", "-1", "1e3", "9" * 30):
        assert gw.client.get(f"/v1/receipts/{bad}", headers=gw.headers()).status_code == 404
        assert gw.client.get(f"/v1/receipts/{bad}/html", headers=gw.headers()).status_code == 404
    page = gw.client.get("/v1/receipts?before=abc", headers=gw.headers())
    assert page.status_code == 200 and page.json() == {"receipts": [], "next_before": None}


def test_workspace_policy_packs_refuse_in_new_sessions(gw: Gateway) -> None:
    from benchpress.packs import POLICY_PACKS_DIR

    bundled = (POLICY_PACKS_DIR / "billing.yaml").read_text(encoding="utf-8")
    assert gw.client.put("/v1/policies/not-yaml", content="::", headers=gw.headers()).status_code == 422
    # A pack's rule ids must start with its name, so renaming the pack renames its rule ids too.
    renamed = bundled.replace("name: billing", "name: team-billing", 1).replace("id: billing.", "id: team-billing.")
    yaml_headers = {**gw.headers(), "content-type": "application/yaml"}
    put = gw.client.put("/v1/policies/team-billing", content=renamed, headers=yaml_headers)
    assert put.status_code == 200, put.text
    listed = gw.client.get("/v1/policies", headers=gw.headers()).json()
    assert "billing" in listed["bundled"] and [p["name"] for p in listed["workspace"]] == ["team-billing"]

    refund = {
        "id": "r1",
        "kind": "create",
        "provider": "stripe",
        "method": "POST",
        "path": "/v1/refunds",
        "body": {"charge": "ch_1"},
    }
    _session(gw, "while-stored")
    body = _execute(gw, "while-stored", refund)
    assert body["status"] == "refused" and body["verdict"]["rule"] == "pack:team-billing.refund-requires-approval"
    assert gw.provider.calls == []

    assert gw.client.delete("/v1/policies/team-billing", headers=gw.headers()).status_code == 204
    _session(gw, "after-delete")
    body = _execute(gw, "after-delete", refund)
    assert body["verdict"]["allowed"] is True and body["status"] == "unverified"
    assert [(c["method"], c["path"]) for c in gw.provider.calls] == [("POST", "/v1/refunds")]


def test_policy_names_must_match_the_path_and_unknown_deletes_are_404(gw: Gateway) -> None:
    pack = "name: local\ntitle: Local\nrules:\n  - {id: local.no-crm, reason: r, match: {providers: [hubspot]}}\n"
    mismatch = gw.client.put("/v1/policies/other", content=pack, headers=gw.headers())
    assert mismatch.status_code == 422 and "'local'" in mismatch.json()["detail"]
    assert gw.client.delete("/v1/policies/local", headers=gw.headers()).status_code == 404


def test_policy_dir_packs_apply_and_a_bad_pack_fails_startup(tmp_path: Path, db_url: str) -> None:
    policy_dir = tmp_path / "packs"
    policy_dir.mkdir()
    (policy_dir / "crm.yaml").write_text(
        "name: no-crm\ntitle: No CRM\nrules:\n"
        "  - {id: no-crm.hubspot, reason: no CRM writes, match: {providers: [hubspot]}}\n"
    )
    with _gateway(db_url, policy_dir=policy_dir) as gw:
        _session(gw)
        body = gw.client.post("/v1/execute", json={"session_id": "s1", "action": WRITE}, headers=gw.headers()).json()
        assert body["verdict"]["rule"] == "pack:no-crm.hubspot" and gw.provider.calls == []
        assert gw.client.get("/v1/policies", headers=gw.headers()).json()["directory"] == ["no-crm"]
    (policy_dir / "broken.yaml").write_text("name: Broken\n")
    with pytest.raises(ConfigError, match="broken.yaml"):
        create_app(Settings(store=db_url, policy_dir=policy_dir))


def test_auth_none_uses_the_default_workspace(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'open.db'}"
    provider = FakeProvider()
    app = create_app(Settings(store=url, auth="none"), executor=provider.execute_tool, clock=lambda: FIXED)
    with TestClient(app) as client:
        body = client.post("/v1/execute", json={"context": {"user_prompt": PROMPT}, "action": WRITE}).json()
        assert body["status"] == "verified"
        assert json.loads(client.get("/v1/meta").text)["auth"] == "none"


async def test_explain_never_executes_and_read_returns_the_interpreted_response(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'svc.db'}"
    store = Store.open(url)
    ws = store.create_workspace("acme")
    provider = FakeProvider()
    app = create_app(Settings(store=url), store=store, executor=provider.execute_tool, clock=lambda: FIXED)
    service: GatewayService = app.state.service
    protected = ContextInput(user_prompt=PROMPT, protected=ProtectedInput(ids=("701",)))
    await service.create_session(ws, SessionCreate(session_id="s1", context=protected))
    action = Action.model_validate(WRITE)

    by_session = await service.explain(ws, action, session_id="s1", context=None)
    assert by_session["allowed"] is False and by_session["rule"] == "protected"
    inline = await service.explain(ws, action, session_id=None, context=ContextInput(user_prompt=PROMPT))
    assert inline == {"allowed": True, "rule": "allowed", "reason": ""}
    for session_id, context in (("missing", None), (None, None)):
        with pytest.raises(RequestRejected) as rejected:
            await service.explain(ws, action, session_id=session_id, context=context)
        assert rejected.value.status in (404, 422)
    assert provider.calls == [] and store.receipts(ws.id) == []
    with store.engine.connect() as connection:
        sessions = connection.execute(text("SELECT count(*) FROM sessions")).scalar_one()
    assert sessions == 1, "an inline explain persists no session"

    path = "/crm/v3/objects/companies/701"
    read = await service.read(ws, "hubspot", path, {"archived": "false"})
    old = {"id": "701", "email": "old@rivermill.example"}
    assert read == {"ok": True, "status_code": 200, "body": old, "error": None}
    assert provider.calls == [{"provider": "hubspot", "method": "GET", "path": path, "query": {"archived": "false"}}]
    store.engine.dispose()


async def test_read_refuses_a_control_plane_path_before_any_provider_call(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'read.db'}"
    store = Store.open(url)
    ws = store.create_workspace("acme")
    provider = FakeProvider()
    app = create_app(Settings(store=url), store=store, executor=provider.execute_tool, clock=lambda: FIXED)
    service: GatewayService = app.state.service
    for path in ("/admin/users", "/_twin/state", "/"):
        with pytest.raises(RequestRejected) as rejected:
            await service.read(ws, "hubspot", path, {})
        assert rejected.value.status == 422
    assert provider.calls == []
    store.engine.dispose()


async def test_the_real_executor_has_no_lifetime_call_cap_and_keeps_no_trace() -> None:
    executor, close = executor_from_settings(Settings(upstreams=(UpstreamConfig("hubspot", "http://127.0.0.1:9"),)))
    assert isinstance(executor, MethodType)
    gateway = executor.__self__
    assert isinstance(gateway, RealAppGateway)
    try:
        blocked = {"provider": "hubspot", "method": "GET", "path": "/admin/state"}  # refused locally, never sent
        results = [await executor(PROVIDER_API, blocked) for _ in range(DEFAULT_MAX_CALLS + 1)]
        errors = {str(as_mapping(result).get("error")) for result in results}
        assert len(errors) == 1 and "call limit" not in next(iter(errors))
        assert gateway.calls == DEFAULT_MAX_CALLS + 1 and gateway.trace == ()
    finally:
        await close()


async def test_without_upstreams_every_provider_call_is_a_503_result() -> None:
    executor, close = executor_from_settings(Settings())
    result = as_mapping(await executor(PROVIDER_API, {"provider": "hubspot", "method": "GET", "path": "/x"}))
    assert result["status_code"] == 503 and "hubspot" in result["error"]
    await close()


def test_an_upstream_the_real_executor_refuses_is_a_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BENCHPRESS_SCRATCH_OK", raising=False)
    with pytest.raises(ConfigError, match="unknown provider"):
        executor_from_settings(Settings(upstreams=(UpstreamConfig("nope", "http://127.0.0.1:9"),)))
    with pytest.raises(ConfigError, match="BENCHPRESS_SCRATCH_OK"):
        executor_from_settings(Settings(upstreams=(UpstreamConfig("hubspot", "https://api.hubapi.example"),)))
