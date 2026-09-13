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


INTERNAL_DOCS = (
    "CLAUDE.md",
    "AGENT-TASKS",
    "BUILD-SPEC",
    "DEMO-SCRIPT",
    "FOUNDERS-EMAIL",
    "HANDOFF",
    "IMPLEMENTATION-PLAN",
    "PLAN-B",
    "RELIABILITY-BRIEF.template",
    "RESEARCH.md",
    "SPRINT-PLAN",
    "STRATEGY.md",
    "SUBMISSION.md",
    "TRACK-DEVSIM",
    "docs/context",
    "/logs/",
)


def _resolves(ref: str) -> bool:
    path = ref.split("#", 1)[0].split("?", 1)[0].strip("/")
    if not path:
        return (SITE / "index.html").is_file()
    return any((SITE / c).is_file() for c in (path, f"{path}.html", f"{path}/index.html"))


def _site_pages() -> list[Path]:
    return sorted(SITE.rglob("*.html"))


def test_local_links_resolve_to_shipped_files() -> None:
    for page in _site_pages():
        for ref in re.findall(r'(?:href|src|srcset)="([^"]+)"', page.read_text(encoding="utf-8")):
            if ref.startswith(("http://", "https://", "#", "mailto:", "data:")):
                continue
            assert _resolves(ref), f"broken local link in {page.relative_to(SITE)}: {ref}"


def test_landing_page_links_the_replay_recording_and_packages() -> None:
    html = (SITE / "index.html").read_text(encoding="utf-8")
    for needle in (
        'href="/replay"',
        'src="/img/demo-terminal.svg"',
        'srcset="/img/demo-terminal-still.svg"',
        "uvx --from benchpress-agent benchpress demo",
        "npm install benchpress-guard",
        "https://pypi.org/project/benchpress-agent/",
        "https://www.npmjs.com/package/benchpress-guard",
        "CHANGELOG.md",
    ):
        assert needle in html, needle


def test_copied_assets_are_byte_identical_to_their_sources() -> None:
    copied: dict[str, str] = build_site.COPIED_ASSETS
    for required in (
        "llms.txt",
        "docs/demo/index.html",
        "docs/img/demo-terminal.svg",
        "docs/img/demo-terminal-still.svg",
    ):
        assert required in copied, required
    for source, destination in copied.items():
        src, dst = REPO_ROOT / source, SITE / destination
        assert dst.is_file(), f"missing copy {destination}"
        assert dst.read_bytes() == src.read_bytes(), f"site/{destination} is stale against {source}"


def test_every_changelog_release_is_on_the_page() -> None:
    releases: list[str] = build_site.changelog_releases()
    assert releases, "no release headings parsed from CHANGELOG.md"
    shipped: dict[str, tuple[str, str]] = build_site.SHIPPED
    assert [r for r in releases if r not in shipped] == []
    html = (SITE / "index.html").read_text(encoding="utf-8")
    for release in releases:
        assert shipped[release][0].replace("'", "&#x27;") in html, release


def test_site_never_links_or_quotes_internal_docs() -> None:
    files = [*_site_pages(), SITE / "llms.txt", SITE / "vercel.json"]
    for file in files:
        text = file.read_text(encoding="utf-8")
        for name in INTERNAL_DOCS:
            assert name not in text, f"{file.relative_to(SITE)} mentions internal doc {name}"


def test_every_anchor_link_has_a_target() -> None:
    html = (SITE / "index.html").read_text(encoding="utf-8")
    ids = set(re.findall(r'\bid="([^"]+)"', html))
    for anchor in re.findall(r'href="#([^"]+)"', html):
        assert anchor in ids, f"#{anchor} has no matching id"


def test_copy_keeps_claim_discipline() -> None:
    text = (SITE / "index.html").read_text(encoding="utf-8") + (SITE / "llms.txt").read_text(encoding="utf-8")
    assert "[X" not in text
    assert "TODO" not in text
    # llms.txt may state the rule itself ('never say "passed ArgaBench"'); only an actual claim fails.
    claims = re.sub(r'never say "passed ArgaBench"', "", text, flags=re.I)
    assert "passed ArgaBench" not in claims
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
