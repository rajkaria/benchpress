"""An in-memory echo executor for demos, smoke tests and load tests. Never for production.

It has no auth, no policy enforcement of its own, no persistence beyond the process's memory, and it
answers every write with 200 regardless of what a real upstream would do — it exists so `benchpress
serve --executor benchpress.gateway.testing:make_echo_executor` (and the load test in Task 14) have
somewhere harmless to point writes and reads at.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast
from urllib.parse import urlsplit

from benchpress.tools import ToolExecutor

__all__ = ["make_echo_executor"]


def _mapping(value: object) -> dict[str, Any]:
    """`value` as a `str`-keyed dict, or `{}` when it is not one."""
    if isinstance(value, Mapping):
        typed = cast(Mapping[object, object], value)
        return {str(key): item for key, item in typed.items()}
    return {}


def make_echo_executor() -> ToolExecutor:
    """A fresh in-memory record store: a write merges its body into a record keyed by path (query dropped);
    a `GET` answers 200 with that record, or 404 when nothing has been written to that path yet.
    """
    records: dict[str, dict[str, Any]] = {}

    async def echo_executor(tool_name: str, tool_input: dict[str, Any]) -> object:
        method = str(tool_input.get("method", "GET")).upper()
        path = urlsplit(str(tool_input.get("path", ""))).path
        if method == "GET":
            record = records.get(path)
            if record is None:
                return {"status_code": 404, "body": {"error": "not found"}}
            return {"status_code": 200, "body": dict(record)}
        body = _mapping(tool_input.get("body"))
        properties = _mapping(body.get("properties", body))
        record = records.setdefault(path, {})
        record.update(properties)
        return {"status_code": 200, "body": dict(record)}

    return echo_executor
