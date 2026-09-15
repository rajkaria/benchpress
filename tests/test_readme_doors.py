"""The README's install table must not promise an npm package under a pip install."""

from __future__ import annotations

from pathlib import Path

README = Path(__file__).resolve().parents[1] / "README.md"


def test_developer_row_names_the_install_for_each_language() -> None:
    content = README.read_text(encoding="utf-8")
    row = next(line for line in content.splitlines() if line.startswith("| **One developer**"))
    install_cell = row.split("|")[2]
    assert "pip install benchpress-agent" in install_cell
    assert "npm i benchpress-guard" in install_cell
