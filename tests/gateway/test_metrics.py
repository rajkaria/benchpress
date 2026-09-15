from __future__ import annotations

from pathlib import Path

from benchpress.gateway.config import Settings
from tests.gateway.test_app import WRITE, _gateway, _session  # pyright: ignore[reportPrivateUsage]


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
