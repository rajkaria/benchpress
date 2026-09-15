"""docs/GATEWAY.md documents every route, CLI command and setting the gateway actually has."""

from __future__ import annotations

import dataclasses
from pathlib import Path

from benchpress.gateway.app import create_app
from benchpress.gateway.config import Settings

ROOT = Path(__file__).resolve().parents[1]
DOC = (ROOT / "docs" / "GATEWAY.md").read_text(encoding="utf-8") if (ROOT / "docs" / "GATEWAY.md").exists() else ""


def test_every_http_route_is_documented(tmp_path: Path) -> None:
    app = create_app(Settings(store=f"sqlite:///{tmp_path / 'd.db'}"))
    paths = sorted(
        {
            getattr(route, "path", "")
            for route in app.routes
            if getattr(route, "path", "").startswith(("/v1", "/healthz", "/metrics"))
        }
    )
    missing = [path for path in paths if f"`{path}`" not in DOC and path not in DOC]
    assert paths and missing == []


def test_every_setting_and_command_is_documented() -> None:
    for field in dataclasses.fields(Settings):
        assert field.name in DOC, field.name
    commands = ("benchpress serve", "benchpress ui", "benchpress db upgrade", "benchpress workspace create", "--http")
    for command in commands:
        assert command in DOC, command


def test_the_readme_team_door_is_available_and_points_at_the_doc() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    row = next(line for line in readme.splitlines() if line.startswith("| **A team**"))
    assert "planned" not in row and "docs/GATEWAY.md" in readme
