"""Locate and import the vendored ArgaBench harness (read-only, never edited).

The harness lives outside this package (`<repo>/arga-twins-benchmark`, a gitignored clone or
symlink; `ARGABENCH_ROOT` overrides it). Its `src/` is put on `sys.path` and its modules are
imported dynamically, so pyright never has to resolve a package that is not installed in this
project's environment. Every attribute read off a harness module is therefore `Any`; the
contract those attributes must satisfy is documented in docs/TRACK-DEVSIM.md §1.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HARNESS_ROOT = REPO_ROOT / "arga-twins-benchmark"
HARNESS_ROOT_ENV = "ARGABENCH_ROOT"
SUITE_TAG = "suite:argabench-40-v1"
SUITE_ID = "argabench-40-v1"
CONTENT_HASH_TAG_PREFIX = "content-sha256:"
RUN_SCRIPT_RELATIVE = Path("scripts") / "run_argabench_40.py"
GRADE_SCRIPT_RELATIVE = Path("scripts") / "grade_argabench_attempt.py"


class HarnessNotFoundError(RuntimeError):
    """The vendored ArgaBench checkout is missing or incomplete."""


def harness_root() -> Path:
    """Return the ArgaBench checkout root, validating that the runner script exists."""

    configured = os.environ.get(HARNESS_ROOT_ENV)
    root = Path(configured).expanduser() if configured else DEFAULT_HARNESS_ROOT
    root = root.resolve()
    if not (root / RUN_SCRIPT_RELATIVE).is_file():
        raise HarnessNotFoundError(
            f"ArgaBench harness not found at {root} (expected {RUN_SCRIPT_RELATIVE}). "
            f"Clone or symlink arga-twins-benchmark into the repo root, or set {HARNESS_ROOT_ENV}."
        )
    return root


def ensure_importable(root: Path | None = None) -> Path:
    """Put `<root>/src` first on `sys.path` (idempotent) and return the root."""

    resolved = root.resolve() if root is not None else harness_root()
    src = str(resolved / "src")
    wanted = Path(src).resolve()
    if not any(Path(entry).resolve() == wanted for entry in sys.path if entry):
        sys.path.insert(0, src)
    return resolved


def harness_module(name: str) -> ModuleType:
    """Import `arga_twins_benchmark.<...>` from the vendored checkout."""

    ensure_importable()
    return importlib.import_module(name)


# --------------------------------------------------------------------------------------
# Benchmark inputs (read-only files under the harness root)
# --------------------------------------------------------------------------------------


def benchmark_dir() -> Path:
    return harness_root() / "benchmark" / "argabench_40"


def suite_path() -> Path:
    return benchmark_dir() / "suite.json"


def tasks_md_path() -> Path:
    return benchmark_dir() / "TASKS.md"


def model_matrix_path() -> Path:
    return benchmark_dir() / "model_matrix.json"


def historical_calibration_path() -> Path:
    return benchmark_dir() / "historical_fable_5_high_fairness_calibration.json"


def scenarios_dir() -> Path:
    return benchmark_dir() / "scenarios"


def read_json_object(path: Path) -> dict[str, Any]:
    payload: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected a JSON object in {path}")
    return cast(dict[str, Any], payload)


def load_suite() -> dict[str, Any]:
    suite = read_json_object(suite_path())
    tasks = suite.get("tasks")
    if suite.get("suite_id") != SUITE_ID or not isinstance(tasks, list) or len(cast(list[object], tasks)) != 40:
        raise ValueError(f"{suite_path()} is not the 40-task {SUITE_ID} suite")
    return suite


def suite_tasks() -> list[dict[str, Any]]:
    return [cast(dict[str, Any], task) for task in cast(list[object], load_suite()["tasks"]) if isinstance(task, dict)]


def suite_task(task_id: str) -> dict[str, Any]:
    wanted = task_id.upper()
    for task in suite_tasks():
        if str(task.get("id")).upper() == wanted:
            return task
    raise KeyError(f"unknown ArgaBench task id {task_id!r}")


def content_hash(task: dict[str, Any]) -> str:
    """The harness's scenario identity: sha256 of the sorted-key, compact task JSON."""

    payload = json.dumps(task, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "CONTENT_HASH_TAG_PREFIX",
    "DEFAULT_HARNESS_ROOT",
    "GRADE_SCRIPT_RELATIVE",
    "HARNESS_ROOT_ENV",
    "REPO_ROOT",
    "RUN_SCRIPT_RELATIVE",
    "SUITE_ID",
    "SUITE_TAG",
    "HarnessNotFoundError",
    "benchmark_dir",
    "content_hash",
    "ensure_importable",
    "harness_module",
    "harness_root",
    "historical_calibration_path",
    "load_suite",
    "model_matrix_path",
    "read_json_object",
    "scenarios_dir",
    "suite_path",
    "suite_task",
    "suite_tasks",
    "tasks_md_path",
]
