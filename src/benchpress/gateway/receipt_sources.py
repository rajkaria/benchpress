"""Where receipts are read from: the gateway reads one workspace's rows in its store."""

from __future__ import annotations

import functools
import json
from typing import Any, Protocol

import anyio.to_thread

from benchpress.gateway.schemas import ReceiptDetail, ReceiptFilters, ReceiptPage, ReceiptSummary
from benchpress.gateway.store import ReceiptRow, Store, WorkspaceRow

__all__ = ["ReceiptSource", "StoreReceiptSource"]


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
