"""The console build is committed and packaged; the gateway and `benchpress ui` both serve it."""

from __future__ import annotations

from pathlib import Path

from benchpress.gateway.console import CONSOLE_DIST


def test_console_dist_is_built_and_self_contained() -> None:
    index = CONSOLE_DIST / "index.html"
    assert index.is_file(), "run: cd packages/console && npm ci && npm run build"
    html = index.read_text(encoding="utf-8")
    assert 'id="root"' in html
    assert "http://" not in html and "https://" not in html, "no external requests from the console shell"
    assets = list((CONSOLE_DIST / "assets").glob("*.js"))
    assert assets and all(Path(a).stat().st_size < 400_000 for a in assets)
