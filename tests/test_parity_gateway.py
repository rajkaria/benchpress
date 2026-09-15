"""Parity: the same 100 calls through the library, the HTTP gateway and the MCP gateway give byte-identical lines."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from benchpress.schemas import validate_write_line
from tests.parity_support import http_lines, library_lines, load_fixture, mcp_lines

ROOT = Path(__file__).resolve().parents[1]


def test_the_committed_fixture_is_what_the_generator_builds() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("gen_gateway_parity", ROOT / "scripts" / "gen_gateway_parity.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    header, calls = module.build_fixture()
    committed_header, committed_calls = load_fixture()
    assert header == committed_header and calls == committed_calls


def test_the_fixture_covers_every_outcome() -> None:
    _, calls = load_fixture()
    assert len(calls) == 100
    assert Counter(call["expect"] for call in calls) == Counter(
        {"verified": 40, "mismatch": 20, "refused": 30, "failed": 5, "unverified": 5}
    )


async def test_library_http_and_mcp_lines_are_byte_identical(tmp_path: Path) -> None:
    fixture = load_fixture()
    library, library_statuses = await library_lines(fixture)
    http, http_statuses = await http_lines(fixture, tmp_path / "http")
    mcp, mcp_statuses = await mcp_lines(fixture, tmp_path / "mcp")
    assert library_statuses == [call["expect"] for call in fixture[1]]
    assert http_statuses == library_statuses and mcp_statuses == library_statuses
    assert len(library) == 100
    for index, (a, b, c) in enumerate(zip(library, http, mcp, strict=True)):
        assert a == b == c, f"call {index}: library/http/mcp lines differ\n{a}\n{b}\n{c}"
        validate_write_line(a)
    assert json.loads(library[0])["workspace"] == "local"


async def test_every_outcome_row_is_reached_for_the_reason_it_names() -> None:
    """The status mix alone would pass if, say, a protected write were refused by another rule; the lines say why."""
    lines, _ = await library_lines(load_fixture())
    payloads = [json.loads(line) for line in lines]
    refused = Counter(payload["verdict"]["rule"] for payload in payloads if payload["status"] == "refused")
    assert refused == Counter(
        {"protected": 8, "external_destination": 6, "method": 3, "control_plane": 3, "idempotency": 10}
    )
    mismatched = Counter(
        "missing" if any(item["observed"] == "missing" for item in payload["evidence"]) else "different"
        for payload in payloads
        if payload["status"] == "mismatch"
    )
    assert mismatched == Counter({"different": 12, "missing": 8})
    unverified = Counter(
        payload["evidence"][0]["observed"] if payload["evidence"] else "no read-back declared"
        for payload in payloads
        if payload["status"] == "unverified"
    )
    assert unverified == Counter({"404: ": 3, "no read-back declared": 2})
    assert {payload["status_code"] for payload in payloads if payload["status"] == "failed"} == {500}
    for index, payload in enumerate(payloads):
        if payload["verdict"]["rule"] == "idempotency":
            assert any(
                earlier["status"] == "verified"
                and earlier["session"] == payload["session"]
                and earlier["action"] == payload["action"]
                for earlier in payloads[:index]
            ), f"call {index} replays no earlier verified call of its session"
