from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import httpx
import pytest

from benchpress.gateway.app import create_app
from benchpress.gateway.config import Settings
from benchpress.gateway.store import Store
from benchpress.gateway.testing import make_echo_executor

ROOT = Path(__file__).resolve().parents[1]


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("loadtest", ROOT / "scripts" / "loadtest.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_percentile_is_nearest_rank() -> None:
    lt = _module()
    assert lt.percentile([5.0, 1.0, 3.0, 2.0, 4.0], 50) == 3.0
    assert lt.percentile([1.0, 2.0, 3.0, 4.0], 99) == 4.0


async def test_in_process_reports_latency_percentiles() -> None:
    result = await _module().in_process(50)
    assert set(result) >= {"n", "p50_ms", "p99_ms", "max_ms"} and result["n"] == 50
    assert 0 < result["p50_ms"] <= result["p99_ms"] <= result["max_ms"]


async def test_against_sends_distinct_writes_and_counts_rate_limited_ones_as_errors(tmp_path: Path) -> None:
    lt = _module()
    url = f"sqlite:///{tmp_path / 'load.db'}"
    store = Store.open(url)
    workspace = store.create_workspace("local")
    _, key = store.create_api_key(workspace.id, "load")
    try:
        async with httpx.AsyncClient() as webhooks:
            roomy = create_app(
                Settings(store=url, requests_per_minute=1_000_000), store=store, executor=make_echo_executor(),
                http=webhooks,
            )
            result = await lt.against(
                "http://gateway.test", key, rps=40, seconds=0.5, concurrency=4, transport=httpx.ASGITransport(roomy)
            )
            assert (result["sent"], result["ok"], result["errors"]) == (20, 20, 0)
            assert 0 < result["p50_ms"] <= result["p99_ms"] <= result["max_ms"] and result["achieved_rps"] > 0
            lines = [json.loads(row.line) for row in store.receipts(workspace.id, limit=500)]
            assert len(lines) == 20 and {line["status"] for line in lines} == {"verified"}
            assert len({line["fingerprint"] for line in lines}) == 20

            # Five requests a minute: the session takes one, four writes get through, every 429 is an error.
            tight = create_app(
                Settings(store=url, requests_per_minute=5), store=store, executor=make_echo_executor(), http=webhooks
            )
            limited = await lt.against(
                "http://gateway.test", key, rps=40, seconds=0.5, concurrency=4, transport=httpx.ASGITransport(tight)
            )
            assert limited["sent"] == 20 and limited["ok"] <= 4
            assert limited["errors"] == limited["http_errors"] == 20 - limited["ok"] >= 16
            assert limited["transport_errors"] == 0
    finally:
        store.engine.dispose()


def test_main_prints_one_json_object_and_needs_a_key_with_a_url(capsys: pytest.CaptureFixture[str]) -> None:
    lt = _module()
    assert lt.main(["--in-process", "20"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["n"] == 20 and 0 < printed["p50_ms"] <= printed["p99_ms"] <= printed["max_ms"]
    with pytest.raises(SystemExit):
        lt.main(["--url", "http://127.0.0.1:8798"])
    assert "--url needs --key" in capsys.readouterr().err
