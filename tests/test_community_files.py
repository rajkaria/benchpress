"""Community files exist, link to each other, and the public roadmap stays public.

`docs/ROADMAP.md` is the public rendering of the sprint plan: it must never name the internal
planning folder or documents, and it must carry a checkbox per sprint task so status is visible
at a glance.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_community_files_exist_and_link_each_other() -> None:
    for name in (
        "CONTRIBUTING.md",
        "SECURITY.md",
        "CODE_OF_CONDUCT.md",
        "docs/ROADMAP.md",
        ".github/PULL_REQUEST_TEMPLATE.md",
        ".github/ISSUE_TEMPLATE/bug.yml",
        ".github/ISSUE_TEMPLATE/playbook.yml",
        ".github/ISSUE_TEMPLATE/config.yml",
    ):
        assert (ROOT / name).exists(), name
    contributing = (ROOT / "CONTRIBUTING.md").read_text()
    assert "Developer Certificate of Origin" in contributing
    assert "uv run pytest -q && uv run ruff check . && uv run pyright" in contributing
    assert "SECURITY.md" in contributing
    assert "CODE_OF_CONDUCT.md" in contributing


EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})")
ALLOWED_EMAIL_DOMAINS = ("contributor-covenant.org", "github.com")
COMMUNITY_FILES = (
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CODE_OF_CONDUCT.md",
    "docs/ROADMAP.md",
    "docs/rfcs/README.md",
    ".github/PULL_REQUEST_TEMPLATE.md",
    ".github/ISSUE_TEMPLATE/bug.yml",
    ".github/ISSUE_TEMPLATE/playbook.yml",
    ".github/ISSUE_TEMPLATE/config.yml",
)


def _unexpected_emails(text: str) -> list[str]:
    found: list[str] = []
    for match in EMAIL.finditer(text):
        address, domain = match.group(0).casefold(), match.group(1).casefold()
        if "noreply" in address:
            continue
        if any(domain == allowed or domain.endswith(f".{allowed}") for allowed in ALLOWED_EMAIL_DOMAINS):
            continue
        found.append(match.group(0))
    return found


def test_the_email_detector_flags_addresses_outside_the_allowlist() -> None:
    assert _unexpected_emails("write to someone@example.org") == ["someone@example.org"]
    assert _unexpected_emails("conduct@contributor-covenant.org, bot@users.noreply.github.com") == []


def test_community_files_contain_no_email_addresses() -> None:
    for name in COMMUNITY_FILES:
        text = (ROOT / name).read_text()
        assert _unexpected_emails(text) == [], name


def test_security_and_conduct_files_point_at_private_channels() -> None:
    security = (ROOT / "SECURITY.md").read_text()
    conduct = (ROOT / "CODE_OF_CONDUCT.md").read_text()
    assert "github.com/rajkaria/benchpress/security/advisories/new" in security
    assert "48 hours" in security
    assert "7 days" in security
    assert "contributor-covenant.org/version/2/1" in conduct


def test_roadmap_has_sprint_checkboxes_and_no_internal_paths() -> None:
    roadmap = (ROOT / "docs" / "ROADMAP.md").read_text()
    assert len(re.findall(r"^- \[[ x]\] ", roadmap, re.MULTILINE)) >= 30
    assert ".internal-docs" not in roadmap
    assert "SPRINT-PLAN" not in roadmap
    assert "worktree" not in roadmap.lower()
    assert "subagent" not in roadmap.lower()
    assert "claude code" not in roadmap.lower() or "claude code plugin" in roadmap.lower()
    assert "## Decisions" in roadmap
    # Sprint 0 items already shipped this sprint are checked off.
    assert re.search(r"- \[x\].*3\.11", roadmap)
    assert re.search(r"- \[x\].*ArgaBench", roadmap)


def test_roadmap_links_contributing_and_issue_tracker() -> None:
    roadmap = (ROOT / "docs" / "ROADMAP.md").read_text()
    assert "CONTRIBUTING.md" in roadmap
    assert "https://github.com/rajkaria/benchpress/issues" in roadmap
