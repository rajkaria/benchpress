"""Where receipts are read from: the gateway reads one workspace's rows in its store; `benchpress ui`
(`DiskReceiptSource`) reads whatever plain JSONL/JSON files a demo, a shim's guard log or a
`VerifiedWrite` sink left on disk.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol, cast

import anyio.to_thread

from benchpress.gateway.schemas import ReceiptDetail, ReceiptFilters, ReceiptKind, ReceiptPage, ReceiptSummary
from benchpress.gateway.store import ReceiptRow, Store, WorkspaceRow, parse_write_line
from benchpress.receipt_html import render_receipt_html

__all__ = ["DiskReceiptSource", "ReceiptSource", "StoreReceiptSource"]


class ReceiptSource(Protocol):
    async def list(self, filters: ReceiptFilters) -> ReceiptPage: ...

    async def get(self, receipt_id: str) -> ReceiptDetail | None: ...

    async def html(self, receipt_id: str) -> str | None: ...


def _summary(row: ReceiptRow) -> ReceiptSummary:
    return ReceiptSummary(
        id=row.id,
        kind="write",
        at=row.at,
        event=row.event,
        session=row.session_id,
        provider=row.provider,
        method=row.method,
        path=row.path,
        status=row.status,
        rule=row.rule,
        resource=row.resource,
    )


class StoreReceiptSource:
    """One workspace's receipts in the gateway store. Every row is a `benchpress-write/1` line."""

    def __init__(self, store: Store, workspace: WorkspaceRow) -> None:
        self._store = store
        self._workspace = workspace

    async def list(self, filters: ReceiptFilters) -> ReceiptPage:
        query = functools.partial(
            self._store.receipts,
            self._workspace.id,
            customer=filters.customer,
            provider=filters.provider,
            rule=filters.rule,
            status=filters.status,
            session=filters.session,
            event=filters.event,
            limit=filters.limit,
            before=filters.before,
        )
        rows = await anyio.to_thread.run_sync(query)
        next_before = rows[-1].id if len(rows) == filters.limit else None
        return ReceiptPage(receipts=[_summary(row) for row in rows], next_before=next_before)

    async def get(self, receipt_id: str) -> ReceiptDetail | None:
        row = await anyio.to_thread.run_sync(self._store.receipt, self._workspace.id, receipt_id)
        if row is None:
            return None
        payload: dict[str, Any] = json.loads(row.line)
        return ReceiptDetail(id=row.id, kind="write", payload=payload, html=False)

    async def html(self, receipt_id: str) -> str | None:
        """Write receipts have no HTML page; only run receipts do."""
        return None


# ---- disk receipts (`benchpress ui`) -----------------------------------------------------------

_SKIP_DIRS = frozenset({"node_modules", ".git", ".venv"})
_WRITE_PROTOCOL = "benchpress-write/1"
_RUN_PROTOCOL = "benchpress-receipt/1"

# One parsed entry: its summary, its kind, and the full JSON payload `get`/`html` return.
_DiskEntry = tuple[ReceiptSummary, ReceiptKind, dict[str, Any]]


def _iter_receipt_files(root: Path) -> list[Path]:
    """Every `*.jsonl`/`*.json` file under `root`, pruning `node_modules`, `.git` and `.venv` directories."""
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in _SKIP_DIRS]
        for name in filenames:
            if name.endswith((".jsonl", ".json")):
                found.append(Path(dirpath) / name)
    return found


def _short_id(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _jsonl_entry(payload: dict[str, Any], receipt_id: str) -> _DiskEntry | None:
    """A `benchpress-write/1` line (kind `write`) or a guard-shim line (kind `guard`); anything else is skipped."""
    if payload.get("protocol") == _WRITE_PROTOCOL:
        try:
            parsed = parse_write_line(payload)
        except (KeyError, TypeError, AttributeError, ValueError):
            # A required key is missing (KeyError), `action` isn't subscriptable (TypeError) or has no
            # `.get` (AttributeError, e.g. `action: null` or a bare string) — a malformed write line is
            # skipped like any other unreadable one, never raised.
            return None
        summary = ReceiptSummary(
            id=receipt_id, kind="write", at=parsed.at, event=parsed.event, session=parsed.session,
            provider=parsed.provider, method=parsed.method, path=parsed.path, status=parsed.status,
            rule=parsed.rule, resource=parsed.resource,
        )
        return summary, "write", payload
    if "tool" in payload and "decision" in payload:
        summary = ReceiptSummary(
            id=receipt_id, kind="guard", at=str(payload.get("ts", "")), event="guard", session="",
            provider="", method="", path=str(payload.get("tool", "")), status=str(payload.get("decision", "")),
            rule=str(payload.get("rule", "")), resource="",
        )
        return summary, "guard", payload
    return None


def _mapping(value: object) -> dict[str, Any]:
    """`value` as a `str`-keyed dict, or `{}`. Narrows an `Any` field from a loaded JSON payload without
    leaving pyright strict looking at an unparameterized `dict` (an isinstance-narrowed `Any` is one)."""
    if isinstance(value, Mapping):
        typed = cast(Mapping[object, object], value)
        return {str(key): item for key, item in typed.items()}
    return {}


def _list(value: object) -> list[object]:
    if isinstance(value, list):
        return cast(list[object], value)
    return []


def _run_entry(payload: dict[str, Any], receipt_id: str) -> _DiskEntry | None:
    """A `benchpress-receipt/1` run receipt (kind `run`); anything else (including a malformed one) is skipped."""
    if payload.get("protocol") != _RUN_PROTOCOL:
        return None
    try:
        providers = _list(_mapping(payload.get("request")).get("providers"))
        targets = _list(payload.get("targets"))
        first_target = _mapping(targets[0]) if targets else {}
        trial_id = str(payload.get("trial_id", ""))
        resource = str(first_target.get("display", "") or "")
        summary = ReceiptSummary(
            id=receipt_id, kind="run", at=str(payload.get("generated_at", "")), event="run", session=trial_id,
            provider=",".join(str(p) for p in providers), method="", path=trial_id,
            status=payload.get("status"), rule="", resource=resource,
        )
    except (TypeError, AttributeError):
        return None
    return summary, "run", payload


class DiskReceiptSource:
    """Receipts scattered across a directory: write and guard JSONL logs, and `receipt.json` run receipts.

    `root` is scanned fresh on every call — there is no cache, no index and no watcher, so a receipt
    written after this source was built is picked up on the very next call. Fine for a demo directory or
    a handful of shim logs; not meant for a directory with millions of lines.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    def _scan(self) -> list[_DiskEntry]:
        entries: list[_DiskEntry] = []
        for path in _iter_receipt_files(self._root):
            relative = path.relative_to(self._root).as_posix()
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue  # unreadable file (missing, permissions, binary/non-UTF-8): skipped, never raised
            if path.name.endswith(".jsonl"):
                for line_number, raw_line in enumerate(text.splitlines(), start=1):
                    stripped = raw_line.strip()
                    if not stripped:
                        continue
                    try:
                        raw = json.loads(stripped)
                    except json.JSONDecodeError:
                        continue  # unreadable line: skipped, never raised
                    # `_mapping` reduces anything that isn't a JSON object to `{}`, which neither branch of
                    # `_jsonl_entry` matches — so a non-object line is skipped exactly like a malformed one.
                    entry = _jsonl_entry(_mapping(raw), _short_id(f"{relative}:{line_number}"))
                    if entry is not None:
                        entries.append(entry)
            else:
                try:
                    raw = json.loads(text)
                except json.JSONDecodeError:
                    continue
                entry = _run_entry(_mapping(raw), _short_id(relative))
                if entry is not None:
                    entries.append(entry)
        return entries

    @staticmethod
    def _matches(summary: ReceiptSummary, filters: ReceiptFilters) -> bool:
        if filters.provider is not None and summary.provider != filters.provider:
            return False
        if filters.rule is not None and summary.rule != filters.rule:
            return False
        if filters.status is not None and summary.status != filters.status:
            return False
        if filters.session is not None and summary.session != filters.session:
            return False
        if filters.event is not None and summary.event != filters.event:
            return False
        if filters.customer is not None:
            term = filters.customer.casefold()
            if term not in summary.resource.casefold() and term not in summary.path.casefold():
                return False
        return True

    async def list(self, filters: ReceiptFilters) -> ReceiptPage:
        entries = await anyio.to_thread.run_sync(self._scan)
        summaries = sorted(
            (entry[0] for entry in entries if self._matches(entry[0], filters)),
            key=lambda summary: (summary.at, summary.id),
            reverse=True,
        )
        if filters.before is not None:
            index = next((i for i, summary in enumerate(summaries) if summary.id == filters.before), None)
            summaries = summaries[index + 1 :] if index is not None else []
        page = summaries[: filters.limit]
        next_before = page[-1].id if len(page) == filters.limit else None
        return ReceiptPage(receipts=page, next_before=next_before)

    async def get(self, receipt_id: str) -> ReceiptDetail | None:
        entries = await anyio.to_thread.run_sync(self._scan)
        for summary, kind, payload in entries:
            if summary.id == receipt_id:
                return ReceiptDetail(id=receipt_id, kind=kind, payload=payload, html=kind == "run")
        return None

    async def html(self, receipt_id: str) -> str | None:
        """`render_receipt_html(payload)` for a `run` receipt; every other kind has no HTML page."""
        entries = await anyio.to_thread.run_sync(self._scan)
        for summary, kind, payload in entries:
            if summary.id == receipt_id and kind == "run":
                return render_receipt_html(payload)
        return None
