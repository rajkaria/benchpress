from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

from fastapi import FastAPI

from benchpress.gateway.config import Settings
from benchpress.gateway.service import GatewayService
from tests.gateway.test_app import WRITE, _gateway, _session  # pyright: ignore[reportPrivateUsage]
from tests.gateway.test_approvals import approvals


def test_metrics_count_writes_refusals_and_routes(tmp_path: Path) -> None:
    with _gateway(f"sqlite:///{tmp_path / 'm.db'}") as gw:
        _session(gw, protected={"ids": ["702"]})
        gw.client.post("/v1/execute", json={"session_id": "s1", "action": WRITE}, headers=gw.headers())
        refused = {**WRITE, "id": "w2", "path": "/crm/v3/objects/companies/702", "readback": None}
        gw.client.post("/v1/execute", json={"session_id": "s1", "action": refused}, headers=gw.headers())
        receipt_id = gw.client.get("/v1/receipts", headers=gw.headers()).json()["receipts"][0]["id"]
        gw.client.get(f"/v1/receipts/{receipt_id}", headers=gw.headers())
        assert gw.client.get("/metrics").status_code == 401
        text = gw.client.get("/metrics", headers=gw.headers()).text
    assert 'benchpress_writes_total{provider="hubspot",status="verified"} 1.0' in text
    assert 'benchpress_refusals_total{rule="protected"} 1.0' in text
    assert 'route="/v1/receipts/{receipt_id}"' in text
    assert f'/v1/receipts/{receipt_id}"' not in text


def test_metrics_can_be_public_on_loopback(tmp_path: Path) -> None:
    with _gateway(f"sqlite:///{tmp_path / 'p.db'}", metrics_auth="none") as gw:
        assert gw.client.get("/metrics").status_code == 200
    assert Settings(metrics_auth="none").host == "127.0.0.1"


def test_an_unknown_provider_is_labelled_other(tmp_path: Path) -> None:
    """Ruling R15: only a bundled playbook provider or a configured upstream may become a label value."""
    with _gateway(f"sqlite:///{tmp_path / 'unk.db'}") as gw:
        _session(gw)
        made_up = {**WRITE, "id": "w-unknown", "provider": "made-up-xyz"}
        gw.client.post("/v1/execute", json={"session_id": "s1", "action": made_up}, headers=gw.headers())
        text = gw.client.get("/metrics", headers=gw.headers()).text
    assert 'benchpress_writes_total{provider="other",status="verified"} 1.0' in text
    assert "made-up-xyz" not in text


def test_metrics_is_rate_limited_like_any_other_route(tmp_path: Path) -> None:
    with _gateway(f"sqlite:///{tmp_path / 'rl.db'}", requests_per_minute=1) as gw:
        first = gw.client.get("/metrics", headers=gw.headers())
        second = gw.client.get("/metrics", headers=gw.headers())
    assert first.status_code == 200
    assert second.status_code == 429 and second.headers["retry-after"] == "1"


def test_approvals_requested_counts_once_per_park_not_per_twin(tmp_path: Path) -> None:
    with approvals(tmp_path) as ap:
        ap.execute()
        ap.execute()  # the same fingerprint, still pending: returns the existing twin, parks nothing new
        text = ap.client.get("/metrics", headers=ap.headers).text
    assert 'benchpress_approvals_total{event="requested"} 1.0' in text


def test_approvals_approved_counts_once_not_on_a_second_409(tmp_path: Path) -> None:
    with approvals(tmp_path) as ap:
        approval_id = ap.execute().json()["approval_id"]
        decision = {"decision": "approve"}
        first = ap.client.post(f"/v1/approvals/{approval_id}", json=decision, headers=ap.headers)
        second = ap.client.post(f"/v1/approvals/{approval_id}", json=decision, headers=ap.headers)
        assert first.status_code == 200 and second.status_code == 409
        text = ap.client.get("/metrics", headers=ap.headers).text
    assert 'benchpress_approvals_total{event="approved"} 1.0' in text


def test_approvals_denied_counts_once(tmp_path: Path) -> None:
    with approvals(tmp_path) as ap:
        approval_id = ap.execute().json()["approval_id"]
        denied = ap.client.post(f"/v1/approvals/{approval_id}", json={"decision": "deny"}, headers=ap.headers)
        assert denied.status_code == 200
        text = ap.client.get("/metrics", headers=ap.headers).text
    assert 'benchpress_approvals_total{event="denied"} 1.0' in text


def test_approvals_expired_counts_on_lazy_resolve_and_on_the_sweep(tmp_path: Path) -> None:
    with approvals(tmp_path) as ap:
        first_id = ap.execute().json()["approval_id"]
        second_write = {**WRITE, "id": "w-second", "path": "/crm/v3/objects/companies/703", "readback": None}
        second_id = ap.execute(second_write).json()["approval_id"]
        assert first_id != second_id
        ap.clock.now += 61

        # lazy expiry: resolving a pending-but-past-TTL approval closes it as expired before refusing the decision.
        late = ap.client.post(f"/v1/approvals/{first_id}", json={"decision": "approve"}, headers=ap.headers)
        assert late.status_code == 409 and late.json()["status"] == "expired"
        text = ap.client.get("/metrics", headers=ap.headers).text
        assert 'benchpress_approvals_total{event="expired"} 1.0' in text

        # the periodic sweep closes the second one without any resolve call; driven directly, no real sleep.
        service = cast(GatewayService, cast(FastAPI, ap.client.app).state.service)
        closed = asyncio.run(service.approvals.expire_due())
        assert closed == 1
        text = ap.client.get("/metrics", headers=ap.headers).text
    assert 'benchpress_approvals_total{event="expired"} 2.0' in text
