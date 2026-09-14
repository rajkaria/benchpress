"""The floor is 3.11: no `type X = ...` statements and no PEP 695 generics in shipped code."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TYPE_STATEMENT = re.compile(r"^\s*type\s+\w+\s*=", re.MULTILINE)
PEP695_GENERIC = re.compile(r"^\s*(async\s+)?def\s+\w+\[", re.MULTILINE)


def test_pyproject_declares_the_floor() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert project["project"]["requires-python"] == ">=3.11"
    assert "Programming Language :: Python :: 3.11" in project["project"]["classifiers"]
    assert project["tool"]["ruff"]["target-version"] == "py311"
    assert project["tool"]["pyright"]["pythonVersion"] == "3.11"


def test_no_312_only_syntax_in_shipped_or_dev_code() -> None:
    offenders: list[str] = []
    for folder in ("src", "devsim", "evals", "scripts"):
        for path in (ROOT / folder).rglob("*.py"):
            text = path.read_text()
            if TYPE_STATEMENT.search(text) or PEP695_GENERIC.search(text):
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []
