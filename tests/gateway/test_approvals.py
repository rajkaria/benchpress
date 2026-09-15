# tests/gateway/test_approvals.py
"""Approvals: park after the gate allows, resume with the same fingerprint, deny, expire, notify."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from benchpress.gateway.app import create_app
from benchpress.gateway.approvals import matching_rule
from benchpress.gateway.config import ApprovalRuleConfig, Settings
from benchpress.gateway.store import Store
from tests.conftest import make_action
from tests.gateway.test_app import FIXED, PROMPT, WRITE
from tests.test_verified import FakeProvider

RULE = ApprovalRuleConfig(
    name="company-writes", provider="hubspot", methods=("PATCH",), path="/crm/v3/objects/companies/*"
)


@dataclass
class Clock:
    now: float = 1_000.0

    def __call__(self) -> float:
        return self.now


@dataclass
class Hooks:
    requests: list[httpx.Request] = field(default_factory=list[httpx.Request])
    fail: bool = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail:
            raise httpx.ConnectError("hook down")
        return httpx.Response(204)


@dataclass
class Approvals:
    client: TestClient
    provider: FakeProvider
    clock: Clock
    hooks: Hooks
    headers: dict[str, str]

    def execute(self, action: dict[str, Any] | None = None):
        # No return annotation: TestClient.post resolves to starlette's vendored httpx2.Response, not httpx.Response.
        body = {"session_id": "s1", "action": action or WRITE}
        return self.client.post("/v1/execute", json=body, headers=self.headers)

    def lines(self) -> list[dict[str, Any]]:
        page = self.client.get("/v1/receipts", headers=self.headers).json()["receipts"]
        details = [self.client.get(f"/v1/receipts/{r['id']}", headers=self.headers).json()["payload"] for r in page]
        return list(reversed(details))


@contextmanager
def approvals(tmp_path: Path, **overrides: Any) -> Generator[Approvals]:
    url = f"sqlite:///{tmp_path / 'ap.db'}"
    store = Store.open(url)
    ws = store.create_workspace("acme")
    _, key = store.create_api_key(ws.id, "approver")
    provider, clock, hooks = FakeProvider(), Clock(), Hooks()
    settings = Settings(
        store=url,
        approval_rules=(RULE,),
        approval_ttl_seconds=60,
        approval_webhook_url="https://hooks.example/bp",
        approval_webhook_secret="s3cret",
        **overrides,
    )
    http = httpx.AsyncClient(transport=httpx.MockTransport(hooks.handler))
    app = create_app(settings, store=store, executor=provider.execute_tool, clock=lambda: FIXED, now=clock, http=http)
    headers = {"Authorization": f"Bearer {key}"}
    with TestClient(app) as client:
        body = {"session_id": "s1", "context": {"user_prompt": PROMPT, "protected": {"ids": ["702"]}}}
        assert client.post("/v1/sessions", json=body, headers=headers).status_code == 201
        yield Approvals(client, provider, clock, hooks, headers)


def test_matching_rule_by_provider_method_path_and_class() -> None:
    patch = make_action("a", provider="hubspot", method="PATCH", path="/crm/v3/objects/companies/701?x=1")
    assert matching_rule((RULE,), patch) == RULE
    assert matching_rule((RULE,), make_action("a", provider="stripe", method="PATCH", path=patch.path)) is None
    assert matching_rule((RULE,), make_action("a", provider="hubspot", method="POST", path=patch.path)) is None
    classed = ApprovalRuleConfig(name="email", classes=("send_email",))
    assert matching_rule((classed,), patch) is None


def test_a_matching_write_is_parked_not_sent(tmp_path: Path) -> None:
    with approvals(tmp_path) as ap:
        response = ap.execute()
        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "needs_approval" and body["approval_id"].startswith("apr_") and body["receipt_id"]
        assert ap.provider.calls == []
        again = ap.execute().json()
        assert again["approval_id"] == body["approval_id"]
        assert [line["event"] for line in ap.lines()] == ["approval_requested"]
        listed = ap.client.get("/v1/approvals?status=pending", headers=ap.headers).json()
        assert [a["id"] for a in listed] == [body["approval_id"]]


def test_approve_resumes_the_same_write(tmp_path: Path) -> None:
    with approvals(tmp_path) as ap:
        approval_id = ap.execute().json()["approval_id"]
        decision = {"decision": "approve", "note": "ok"}
        resolved = ap.client.post(f"/v1/approvals/{approval_id}", json=decision, headers=ap.headers)
        assert resolved.status_code == 200 and resolved.json()["status"] == "verified"
        assert [c["method"] for c in ap.provider.calls] == ["PATCH", "GET"]
        lines = ap.lines()
        assert [line["event"] for line in lines] == ["approval_requested", "approval_resolved", "write"]
        assert len({line["fingerprint"] for line in lines}) == 1
        assert lines[1]["approval"] == {"id": approval_id, "decision": "approve", "by": "approver", "note": "ok"}
        assert lines[2]["approval"] == {"id": approval_id, "decision": "approve", "by": "approver"}
        second = ap.client.post(f"/v1/approvals/{approval_id}", json={"decision": "approve"}, headers=ap.headers)
        assert second.status_code == 409 and second.json()["status"] == "approved"
        assert len(ap.provider.calls) == 2


def test_deny_never_reaches_the_provider(tmp_path: Path) -> None:
    with approvals(tmp_path) as ap:
        approval_id = ap.execute().json()["approval_id"]
        denied = ap.client.post(f"/v1/approvals/{approval_id}", json={"decision": "deny"}, headers=ap.headers).json()
        assert denied["status"] == "denied" and ap.provider.calls == []
        assert [line["event"] for line in ap.lines()] == ["approval_requested", "approval_resolved"]


def test_an_expired_approval_cannot_be_approved(tmp_path: Path) -> None:
    with approvals(tmp_path) as ap:
        approval_id = ap.execute().json()["approval_id"]
        ap.clock.now += 61
        late = ap.client.post(f"/v1/approvals/{approval_id}", json={"decision": "approve"}, headers=ap.headers)
        assert late.status_code == 409 and late.json()["status"] == "expired"
        assert ap.provider.calls == []
        assert ap.lines()[-1]["approval"]["decision"] == "expired"


def test_a_gate_refusal_is_never_parked(tmp_path: Path) -> None:
    with approvals(tmp_path) as ap:
        protected = {**WRITE, "id": "w9", "path": "/crm/v3/objects/companies/702", "readback": None}
        body = ap.execute(protected).json()
        assert body["status"] == "refused" and body["approval_id"] is None
        assert ap.client.get("/v1/approvals", headers=ap.headers).json() == []


def test_the_webhook_is_signed(tmp_path: Path) -> None:
    with approvals(tmp_path) as ap:
        ap.execute()
        (request,) = ap.hooks.requests
        assert request.headers["x-benchpress-event"] == "approval_requested"
        expected = hmac.new(b"s3cret", request.content, hashlib.sha256).hexdigest()
        assert request.headers["x-benchpress-signature"] == f"sha256={expected}"
        assert json.loads(request.content)["approval"]["fingerprint"]


def test_a_failing_webhook_does_not_block_parking(tmp_path: Path) -> None:
    with approvals(tmp_path) as ap:
        ap.hooks.fail = True
        assert ap.execute().status_code == 202


def test_other_workspaces_cannot_see_or_resolve(tmp_path: Path) -> None:
    with approvals(tmp_path) as ap:
        approval_id = ap.execute().json()["approval_id"]
        app = cast(FastAPI, ap.client.app)
        store: Store = cast(Store, app.state.store)
        other = store.create_workspace("other")
        _, key = store.create_api_key(other.id, "o")
        headers = {"Authorization": f"Bearer {key}"}
        approve = ap.client.post(f"/v1/approvals/{approval_id}", json={"decision": "approve"}, headers=headers)
        assert approve.status_code == 404
        assert ap.client.get(f"/v1/approvals/{approval_id}", headers=headers).status_code == 404
