"""The replay page renders from a real `benchpress demo` run and loads nothing from the network."""

from __future__ import annotations

import asyncio
import html
import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from benchpress.demo import run_demo

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("render_replay_page", REPO_ROOT / "scripts" / "render_replay_page.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["render_replay_page"] = module
    spec.loader.exec_module(module)
    return module


page_mod = _load()


@pytest.fixture(scope="module")
def run(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, Any], str]:
    run_dir = tmp_path_factory.mktemp("page-demo")
    asyncio.run(run_demo(run_dir))
    out = page_mod.write_page(run_dir, run_dir / "index.html")
    receipt: dict[str, Any] = json.loads((run_dir / "receipt.json").read_text(encoding="utf-8"))
    return run_dir, receipt, out.read_text(encoding="utf-8")


def test_refused_lookalike_action_and_rule_are_shown(run: tuple[Path, dict[str, Any], str]) -> None:
    _, receipt, page = run
    refusals: list[dict[str, Any]] = receipt["refusals"]
    assert refusals, "the demo plants a write to the look-alike; the gate must refuse it"
    for verdict in refusals:
        assert f'aria-label="Refused write {verdict["action_id"]}"' in page
        assert f'<code class="big">{html.escape(verdict["action_id"])}</code>' in page
        assert f'<code class="big">{html.escape(verdict["rule"])}</code>' in page
        assert html.escape(verdict["reason"], quote=True) in page
    for locked in receipt["protected"]["ids"]:
        assert f"<code>{html.escape(locked)}</code>" in page


def test_readback_expected_and_observed_values_are_shown(run: tuple[Path, dict[str, Any], str]) -> None:
    _, receipt, page = run
    readbacks = [item for item in receipt["evidence"] if item["check"].startswith("readback:")]
    assert readbacks
    for item in readbacks:
        assert html.escape(item["check"]) in page
        assert html.escape(item["expected"], quote=True) in page
        assert html.escape(item["observed"], quote=True) in page
    assert f'pill-lg">{receipt["status"]}<' in page


def test_policy_candidates_draft_and_phases_come_from_the_run(run: tuple[Path, dict[str, Any], str]) -> None:
    _, receipt, page = run
    for policy in receipt["policies"]:
        assert html.escape(policy["quote"], quote=True) in page
    for target in receipt["targets"]:
        assert html.escape(target["resource_id"]) in page
    assert "UNSENT" in page
    assert html.escape(receipt["request"]["prompt"], quote=True) in page
    for phase in ("P0", "P1", "P2", "P3", "P4", "P5", "P6", "P7"):
        assert f'id="panel-{phase.lower()}"' in page
    assert "uvx --from benchpress-agent benchpress demo" in page
    assert "scripted model" in page


def test_ticker_counts_match_the_trace(run: tuple[Path, dict[str, Any], str]) -> None:
    run_dir, receipt, page = run
    trace = [line for line in (run_dir / "benchpress-trace.jsonl").read_text().splitlines() if line.strip()]
    rows = re.findall(r'<tr data-phase="P\d"', page)
    assert len(rows) == len(trace) + len(receipt["refusals"])
    assert f"{len(trace)} calls" in page


def test_no_network_resource_loads(run: tuple[Path, dict[str, Any], str]) -> None:
    _, _, page = run
    allowed_font_host = ("https://fonts.googleapis.com", "https://fonts.gstatic.com")
    for match in re.finditer(r"https?://[^\s\"'<>)]+", page):
        url = match.group(0)
        before = page[max(0, match.start() - 6) : match.start()]
        is_plain_link = before.endswith('href="') and page.rfind("<a ", 0, match.start()) > page.rfind(
            "<link", 0, match.start()
        )
        assert is_plain_link or url.startswith(allowed_font_host), f"network resource in page: {url}"
    assert not re.search(r"<(script|img|iframe|video|audio|source)[^>]*\ssrc=", page)
    assert not re.search(r"@import|url\(\s*['\"]?https?:", page)
    assert "<link" not in page.replace('<link rel="icon" href="data:,">', "")


def test_escapes_hostile_values() -> None:
    receipt: dict[str, Any] = {
        "status": "completed",
        "request": {"prompt": "<script>alert(1)</script>", "frame": {}},
        "refusals": [{"action_id": "<b>x</b>", "rule": "protected", "reason": "<img src=x>"}],
    }
    page = page_mod.render_page(receipt, [])
    assert "<script>alert(1)</script>" not in page
    assert "<img src=x>" not in page


def test_committed_page_is_current(run: tuple[Path, dict[str, Any], str]) -> None:
    """The demo is deterministic, so the committed page must equal a fresh render byte for byte."""
    _, _, page = run
    committed = REPO_ROOT / "docs" / "demo" / "index.html"
    hint = "stale: `uv run benchpress demo --trace-dir runs/page-demo && uv run python scripts/render_replay_page.py`"
    assert committed.is_file(), hint
    assert committed.read_text(encoding="utf-8") == page, hint
