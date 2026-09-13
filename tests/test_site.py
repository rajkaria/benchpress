"""The landing page in `site/` stays in sync with `reports/` and never links to a file it doesn't ship."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[1]
SITE = REPO_ROOT / "site"


def _load_build_site() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_site", REPO_ROOT / "scripts" / "build_site.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_site"] = module
    spec.loader.exec_module(module)
    return module


build_site = _load_build_site()


def test_generated_blocks_and_assets_match_reports() -> None:
    stale: list[str] = build_site.build(check=True)
    assert stale == [], f"site/ is stale, run `uv run python scripts/build_site.py`: {stale}"


def test_local_links_resolve_to_shipped_files() -> None:
    html = (SITE / "index.html").read_text(encoding="utf-8")
    for ref in re.findall(r'(?:href|src)="([^"]+)"', html):
        if ref.startswith(("http://", "https://", "#", "mailto:")):
            continue
        path = ref.split("#", 1)[0].lstrip("/") or "index.html"
        assert (SITE / path).is_file() or (SITE / f"{path}.html").is_file(), f"broken local link: {ref}"


def test_every_anchor_link_has_a_target() -> None:
    html = (SITE / "index.html").read_text(encoding="utf-8")
    ids = set(re.findall(r'\bid="([^"]+)"', html))
    for anchor in re.findall(r'href="#([^"]+)"', html):
        assert anchor in ids, f"#{anchor} has no matching id"


def test_copy_keeps_claim_discipline() -> None:
    text = (SITE / "index.html").read_text(encoding="utf-8") + (SITE / "llms.txt").read_text(encoding="utf-8")
    assert "[X" not in text
    assert "TODO" not in text
    assert "passed ArgaBench" not in text
    assert "official leaderboard" in text


def test_render_marks_missing_arms_instead_of_inventing_a_score() -> None:
    summary = {
        "real_trials": [
            {"substrate": "real", "arm": "benchpress", "trial": "t1", "outcome": "unsafe", "failing_assertions": ["A4"]}
        ],
        "twin_trials": [],
    }
    html: str = build_site.render_scoreboard(build_site.collect_trials(summary))
    assert "not run yet" in html
    assert "0<span" in html
    assert "unsafe" in html
