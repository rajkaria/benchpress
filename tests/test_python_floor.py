"""The floor is 3.11: no `type X = ...` statements and no PEP 695 generics (functions or classes) anywhere."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TYPE_STATEMENT = re.compile(r"^\s*type\s+\w+\s*=", re.MULTILINE)
PEP695_GENERIC = re.compile(r"^\s*(async\s+)?def\s+\w+\[", re.MULTILINE)
PEP695_CLASS = re.compile(r"^\s*class\s+\w+\[", re.MULTILINE)
SCANNED = ("src", "tests", "devsim", "evals", "scripts")


def test_pyproject_declares_the_floor() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert project["project"]["requires-python"] == ">=3.11"
    assert "Programming Language :: Python :: 3.11" in project["project"]["classifiers"]
    assert project["tool"]["ruff"]["target-version"] == "py311"
    assert project["tool"]["pyright"]["pythonVersion"] == "3.11"


def _uses_312_syntax(text: str) -> bool:
    return any(pattern.search(text) for pattern in (TYPE_STATEMENT, PEP695_GENERIC, PEP695_CLASS))


def test_the_detector_catches_every_312_only_form() -> None:
    assert _uses_312_syntax("type Pair = tuple[int, int]\n")
    assert _uses_312_syntax("def first[T](items: list[T]) -> T: ...\n")
    assert _uses_312_syntax("async def first[T](items: list[T]) -> T: ...\n")
    assert _uses_312_syntax("class Box[T]:\n    item: T\n")
    assert _uses_312_syntax("    class Inner[K, V](Base):\n        pass\n")
    assert not _uses_312_syntax("class Box(Generic[T]):\n    item: T\n")
    assert not _uses_312_syntax("items: list[int] = []\n")


def test_no_312_only_syntax_in_shipped_dev_or_test_code() -> None:
    offenders: list[str] = []
    for folder in SCANNED:
        for path in (ROOT / folder).rglob("*.py"):
            if _uses_312_syntax(path.read_text()):
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []
