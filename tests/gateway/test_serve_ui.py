"""`serve` builds the gateway from config and bootstraps a key; `ui` serves the same console over disk receipts."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from benchpress import cli
from benchpress.context import Context
from benchpress.gateway.app import create_ui_app
from benchpress.gateway.console import mount_console
from benchpress.gateway.testing import make_echo_executor
from benchpress.shims.guard_policy import GuardPolicy, PolicyGuard, classify_tool_name
from benchpress.verified import VerifiedWrite
from benchpress.write_receipts import JsonlReceiptSink
from tests.test_verified import _PROMPT, _write  # pyright: ignore[reportPrivateUsage]


def _serve_args(tmp_path: Path, *extra: str) -> argparse.Namespace:
    parser = cli.build_parser()
    return parser.parse_args(["serve", "--store", f"sqlite:///{tmp_path / 'serve.db'}", *extra])


def test_serve_bootstraps_a_key_once(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    app, settings = cli.build_server(_serve_args(tmp_path), env={})
    assert settings.port == 8787 and app.title
    key_line = capsys.readouterr().err
    assert "API key (shown once): bp_" in key_line
    cli.build_server(_serve_args(tmp_path), env={})
    assert "bp_" not in capsys.readouterr().err


def test_serve_refuses_open_auth_off_loopback(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["serve", "--store", f"sqlite:///{tmp_path / 'x.db'}", "--auth", "none", "--host", "0.0.0.0"])
    assert code == 2 and "auth" in capsys.readouterr().err


def test_serve_runs_uvicorn_with_the_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_run(app: object, *, host: str, port: int, **kwargs: object) -> None:
        seen.update(host=host, port=port, app=app)

    monkeypatch.setattr("uvicorn.run", fake_run)
    code = cli.main(["serve", "--store", f"sqlite:///{tmp_path / 'r.db'}", "--port", "9123",
                     "--executor", "benchpress.gateway.testing:make_echo_executor"])
    assert code == 0 and seen["port"] == 9123 and seen["host"] == "127.0.0.1"


async def test_echo_executor_supports_a_verified_write() -> None:
    outcome = await VerifiedWrite(make_echo_executor(), context=Context(user_prompt=_PROMPT)).run(_write())
    assert outcome.status == "verified"


async def _receipts_dir(tmp_path: Path) -> Path:
    from benchpress.demo import run_demo

    root = tmp_path / "receipts"
    writer = VerifiedWrite(make_echo_executor(), context=Context(user_prompt=_PROMPT),
                           receipts=JsonlReceiptSink(root / "writes.jsonl"), clock=lambda: "2026-09-14T00:00:01.000Z")
    await writer.run(_write())
    guard = PolicyGuard(GuardPolicy(), receipts_path=root / "guard.jsonl")
    decision = guard.decide_class("delete_invoice", classify_tool_name("delete_invoice"), {})
    guard.record("delete_invoice", {"id": "INV-1"}, decision, 0.0)
    await run_demo(root / "run")
    (root / "notes.jsonl").write_text("not json\n{\"other\": 1}\n", encoding="utf-8")
    return root


async def test_ui_lists_write_guard_and_run_receipts(tmp_path: Path) -> None:
    root = await _receipts_dir(tmp_path)
    with TestClient(create_ui_app(root)) as client:
        assert client.get("/v1/meta").json()["mode"] == "local"
        page = client.get("/v1/receipts").json()["receipts"]
        assert sorted(r["kind"] for r in page) == ["guard", "run", "write"]
        by_kind = {r["kind"]: r for r in page}
        assert by_kind["guard"]["status"] == "refuse" and by_kind["write"]["status"] == "verified"
        run_id = by_kind["run"]["id"]
        assert client.get(f"/v1/receipts/{run_id}").json()["html"] is True
        html = client.get(f"/v1/receipts/{run_id}/html")
        assert html.status_code == 200 and "<html" in html.text.lower()
        assert client.get(f"/v1/receipts/{by_kind['write']['id']}/html").status_code == 404
        assert [r["kind"] for r in client.get("/v1/receipts?event=guard").json()["receipts"]] == ["guard"]
        assert client.get("/v1/receipts/0000000000000000").status_code == 404


def test_ui_refuses_non_loopback(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["ui", "--host", "0.0.0.0"]) == 2
    assert "loopback" in capsys.readouterr().err


def test_console_mount_serves_built_assets_or_explains_the_build(tmp_path: Path) -> None:
    from fastapi import FastAPI

    built = tmp_path / "dist"
    built.mkdir()
    (built / "index.html").write_text('<div id="root"></div>', encoding="utf-8")
    app = FastAPI()

    @app.get("/v1/meta")
    def meta() -> dict[str, str]:
        return {"mode": "test"}

    mount_console(app, built)
    with TestClient(app) as client:
        assert 'id="root"' in client.get("/").text and client.get("/v1/meta").json() == {"mode": "test"}
    bare = FastAPI()
    mount_console(bare, tmp_path / "missing")
    with TestClient(bare) as client:
        assert "npm run build" in client.get("/").text
