"""The approval queue: a gate-allowed write an approval rule matches is parked, not sent.

`ApprovalQueue` owns the lifecycle of one parked write: `park` on a rule match, `resolve` to approve, deny
or discover it has expired, and `expire_due` for the periodic sweep. Every state change appends a
`benchpress-write/1` line (`approval_requested` or `approval_resolved`) through `write_receipts.receipt_line`,
so the receipt stream is the single source of truth for what happened and when.
"""

from __future__ import annotations

import functools
import hashlib
import hmac
import json
import logging
import secrets
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from typing import Literal, cast

import anyio.to_thread
import httpx

from benchpress.context import Action, GateVerdict
from benchpress.gate import classify, fingerprint
from benchpress.gateway.config import ApprovalRuleConfig
from benchpress.gateway.schemas import ApprovalView
from benchpress.gateway.store import ApprovalRow, Store, WorkspaceRow
from benchpress.write_receipts import Clock, receipt_line

__all__ = ["ApprovalClosed", "ApprovalNotFound", "ApprovalQueue", "approval_view", "iso", "matching_rule"]

_logger = logging.getLogger("benchpress.gateway")

_DECISION_FOR_STATUS: dict[str, str] = {"approved": "approve", "denied": "deny", "expired": "expired"}


def iso(epoch: float) -> str:
    """`epoch` (seconds) as an ISO-8601 UTC timestamp with millisecond precision, matching `context.utc_now`."""
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def matching_rule(rules: Sequence[ApprovalRuleConfig], action: Action) -> ApprovalRuleConfig | None:
    """The first rule (in file order) whose provider, method, path and classes all match `action`."""
    path = action.path.split("?", 1)[0]
    method = action.method.upper()
    labels = classify(action.provider, action.method, action.path, action.body)
    for rule in rules:
        if not fnmatchcase(action.provider, rule.provider):
            continue
        if method not in {m.upper() for m in rule.methods}:
            continue
        if not fnmatchcase(path, rule.path):
            continue
        if rule.classes and not (set(rule.classes) & labels):
            continue
        return rule
    return None


def approval_view(row: ApprovalRow) -> ApprovalView:
    return ApprovalView(
        id=row.id,
        session_id=row.session_id,
        rule=row.rule_name,
        status=row.status,
        requested_at=row.requested_at,
        expires_at=iso(row.expires_at),
        resolved_at=row.resolved_at,
        resolved_by=row.resolved_by,
        note=row.note,
        action=row.action,
        fingerprint=row.fingerprint,
    )


class ApprovalNotFound(LookupError):
    """No approval with this id in the caller's workspace (an unknown id and another workspace's id look alike)."""


class ApprovalClosed(RuntimeError):
    """Raised by `resolve` when the approval is not (or no longer) pending; `status` carries its current status."""

    def __init__(self, status: str) -> None:
        super().__init__(f"approval is {status}")
        self.status = status


class ApprovalQueue:
    """Parks gate-allowed writes an approval rule matches, and resolves them by approve, deny or expiry."""

    def __init__(
        self,
        store: Store,
        *,
        ttl_seconds: int,
        webhook_url: str | None,
        webhook_secret: str | None,
        now: Callable[[], float],
        clock: Clock,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._store = store
        self._ttl_seconds = ttl_seconds
        self._webhook_url = webhook_url
        self._webhook_secret = webhook_secret
        self._now = now
        self._clock = clock
        self._http = http

    # ---- park -----------------------------------------------------------------------------

    async def park(
        self, workspace: WorkspaceRow, session_id: str, action: Action, verdict: GateVerdict, rule: ApprovalRuleConfig
    ) -> tuple[ApprovalRow, str | None]:
        """Park `action`, or return the twin already pending for this exact fingerprint in this session."""
        fp = fingerprint(action)
        existing = await anyio.to_thread.run_sync(self._store.pending_approval_for, workspace.id, session_id, fp)
        if existing is not None:
            if self._now() < existing.expires_at:
                receipt_id = await anyio.to_thread.run_sync(
                    self._requested_receipt_id, workspace.id, session_id, existing.id
                )
                return existing, receipt_id
            await self._close(workspace, existing, status="expired", by="system", note="")

        approval_id = f"apr_{secrets.token_hex(8)}"
        requested_at = self._clock()
        expires_at = self._now() + self._ttl_seconds
        row = ApprovalRow(
            id=approval_id,
            workspace_id=workspace.id,
            session_id=session_id,
            action=action,
            fingerprint=fp,
            rule_name=rule.name,
            status="pending",
            requested_at=requested_at,
            expires_at=expires_at,
            resolved_at=None,
            resolved_by=None,
            note="",
        )
        await anyio.to_thread.run_sync(self._store.insert_approval, row)
        line = receipt_line(
            event="approval_requested",
            at=requested_at,
            workspace=workspace.name,
            session=session_id,
            action=action,
            verdict=verdict,
            status=None,
            status_code=None,
            evidence=(),
            approval={"id": approval_id, "rule": rule.name, "expires_at": iso(expires_at)},
        )
        append = functools.partial(self._store.append_receipt, workspace.id, session_id, line)
        receipt_row = await anyio.to_thread.run_sync(append)
        await self._notify(row, workspace)
        return row, receipt_row.id

    def _requested_receipt_id(self, workspace_id: str, session_id: str, approval_id: str) -> str | None:
        """Ruling R14: the receipt id of the `approval_requested` line whose `approval.id` is `approval_id`."""
        rows = self._store.receipts(workspace_id, session=session_id, event="approval_requested", limit=1000)
        for row in rows:
            payload = cast(dict[str, object], json.loads(row.line))
            approval = payload.get("approval")
            if isinstance(approval, dict):
                approval_map = cast(dict[str, object], approval)
                if approval_map.get("id") == approval_id:
                    return row.id
        return None

    # ---- resolve ----------------------------------------------------------------------------

    async def resolve(
        self,
        workspace: WorkspaceRow,
        approval_id: str,
        *,
        decision: Literal["approve", "deny"],
        by: str,
        note: str = "",
    ) -> ApprovalRow:
        row = await anyio.to_thread.run_sync(self._store.approval, workspace.id, approval_id)
        if row is None:
            raise ApprovalNotFound(approval_id)
        if row.status == "pending" and self._now() >= row.expires_at:
            await self._close(workspace, row, status="expired", by="system", note="")
            raise ApprovalClosed("expired")
        if row.status != "pending":
            raise ApprovalClosed(row.status)
        status = "approved" if decision == "approve" else "denied"
        closed = await self._close(workspace, row, status=status, by=by, note=note)
        if closed is None:
            current = await anyio.to_thread.run_sync(self._store.approval, workspace.id, approval_id)
            raise ApprovalClosed(current.status if current is not None else "missing")
        return closed

    async def _close(
        self, workspace: WorkspaceRow, row: ApprovalRow, *, status: str, by: str, note: str
    ) -> ApprovalRow | None:
        """Compare-and-set `row` from `pending` to `status`; append `approval_resolved` only when it wins the race."""
        resolved_at = self._clock()
        update = functools.partial(
            self._store.update_approval, row.id, status=status, resolved_at=resolved_at, resolved_by=by, note=note
        )
        updated = await anyio.to_thread.run_sync(update)
        if not updated:
            return None
        verdict = GateVerdict(action_id=row.action.id, allowed=True, rule="allowed")
        approval_payload = {"id": row.id, "decision": _DECISION_FOR_STATUS[status], "by": by, "note": note}
        line = receipt_line(
            event="approval_resolved",
            at=resolved_at,
            workspace=workspace.name,
            session=row.session_id,
            action=row.action,
            verdict=verdict,
            status=None,
            status_code=None,
            evidence=(),
            approval=approval_payload,
        )
        append = functools.partial(self._store.append_receipt, workspace.id, row.session_id, line)
        await anyio.to_thread.run_sync(append)
        return replace(row, status=status, resolved_at=resolved_at, resolved_by=by, note=note)

    # ---- expiry sweep -------------------------------------------------------------------------

    async def expire_due(self, workspace_id: str | None = None) -> int:
        """Close every pending approval at or past its `expires_at`. Returns how many this call actually closed."""
        due = await anyio.to_thread.run_sync(self._due_pending, workspace_id)
        closed = 0
        for workspace, row in due:
            if await self._close(workspace, row, status="expired", by="system", note="") is not None:
                closed += 1
        return closed

    def _due_pending(self, workspace_id: str | None) -> list[tuple[WorkspaceRow, ApprovalRow]]:
        now = self._now()
        if workspace_id is not None:
            found = self._store.workspace(workspace_id)
            workspaces = [found] if found is not None else []
        else:
            workspaces = self._store.workspaces()
        due: list[tuple[WorkspaceRow, ApprovalRow]] = []
        for workspace in workspaces:
            for row in self._store.approvals(workspace.id, status="pending"):
                if now >= row.expires_at:
                    due.append((workspace, row))
        return due

    # ---- webhook (best effort) -----------------------------------------------------------------

    async def _notify(self, row: ApprovalRow, workspace: WorkspaceRow) -> None:
        if self._webhook_url is None or self._http is None:
            return
        payload = {
            "event": "approval_requested",
            "approval": approval_view(row).model_dump(mode="json"),
            "workspace": workspace.name,
        }
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        headers = {"Content-Type": "application/json", "X-Benchpress-Event": "approval_requested"}
        if self._webhook_secret is not None:
            signature = hmac.new(self._webhook_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
            headers["X-Benchpress-Signature"] = f"sha256={signature}"
        try:
            response = await self._http.post(self._webhook_url, content=body, headers=headers, timeout=5.0)
        except Exception:
            # Any failure to send (a transport error, a bad URL, a client already closed, ...) is logged and
            # never fails the park; `Exception` never includes the cancellation exception, so that propagates.
            _logger.warning("approval webhook request failed for approval %s", row.id)
            return
        if not (200 <= response.status_code < 300):
            _logger.warning("approval webhook returned status %s for approval %s", response.status_code, row.id)
