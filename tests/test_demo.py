"""`benchpress demo` runs offline end to end and every check it prints passes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchpress.cli import main


def test_demo_command_runs_offline_and_writes_receipts(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "demo"
    assert main(["demo", "--trace-dir", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "[FAIL]" not in printed and printed.count("[PASS]") == 7
    assert "planted write to the look-alike refused by the gate" in printed
    receipt = json.loads((out / "receipt.json").read_text())
    assert receipt
    assert (out / "receipt.html").read_text().lstrip().lower().startswith("<!doctype html")
