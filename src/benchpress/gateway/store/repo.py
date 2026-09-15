"""`Store`: the gateway's repository over the tables in `models.py`.

Every method opens its own `sqlalchemy.orm.Session` and commits (or rolls back) before returning, so a
`Store` is safe to share across requests and threads — nothing here holds a transaction open between
calls. Row dataclasses (frozen, never bound to a session) are what leaves this module; callers never see
an ORM instance.
"""

from __future__ import annotations

import functools
import hashlib
import json
import re
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

import anyio.to_thread
from sqlalchemy import CursorResult, delete, insert, or_, select, text
from sqlalchemy import update as sa_update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DataError, IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session as OrmSession

from benchpress.context import Action, Context, utc_now
from benchpress.gateway.store.engine import make_engine, run_upgrade
from benchpress.gateway.store.models import ApiKey, Approval, Policy, Receipt, SessionModel, Workspace, Write
from benchpress.idempotency import ClaimResult

__all__ = [
    "ApiKeyRow",
    "ApprovalRow",
    "ParsedWrite",
    "PolicyRow",
    "ReceiptRow",
    "SessionExists",
    "SqlIdempotencyStore",
    "Store",
    "WorkspaceRow",
    "parse_write_line",
]


class SessionExists(ValueError):
    """Raised by `Store.create_session` when `(workspace_id, session_id)` already exists."""


@dataclass(frozen=True)
class WorkspaceRow:
    id: str
    name: str
    created_at: str


@dataclass(frozen=True)
class ApiKeyRow:
    id: str
    workspace_id: str
    name: str
    prefix: str
    created_at: str
    revoked_at: str | None


@dataclass(frozen=True)
class ReceiptRow:
    id: str
    workspace_id: str
    session_id: str
    event: str
    at: str
    provider: str
    method: str
    path: str
    status: str | None
    rule: str
    resource: str
    line: str


@dataclass(frozen=True)
class ApprovalRow:
    id: str
    workspace_id: str
    session_id: str
    action: Action
    fingerprint: str
    rule_name: str
    status: str
    requested_at: str
    expires_at: float
    resolved_at: str | None
    resolved_by: str | None
    note: str


@dataclass(frozen=True)
class PolicyRow:
    workspace_id: str
    name: str
    body: str
    updated_at: str


def _workspace_row(row: Workspace) -> WorkspaceRow:
    return WorkspaceRow(id=row.id, name=row.name, created_at=row.created_at)


def _api_key_row(row: ApiKey) -> ApiKeyRow:
    return ApiKeyRow(
        id=row.id,
        workspace_id=row.workspace_id,
        name=row.name,
        prefix=row.prefix,
        created_at=row.created_at,
        revoked_at=row.revoked_at,
    )


def _receipt_row(row: Receipt) -> ReceiptRow:
    return ReceiptRow(
        id=str(row.id),
        workspace_id=row.workspace_id,
        session_id=row.session_id,
        event=row.event,
        at=row.at,
        provider=row.provider,
        method=row.method,
        path=row.path,
        status=row.status,
        rule=row.rule,
        resource=row.resource,
        line=row.line,
    )


def _approval_row(row: Approval) -> ApprovalRow:
    return ApprovalRow(
        id=row.id,
        workspace_id=row.workspace_id,
        session_id=row.session_id,
        action=Action.model_validate_json(row.action),
        fingerprint=row.fingerprint,
        rule_name=row.rule_name,
        status=row.status,
        requested_at=row.requested_at,
        expires_at=row.expires_at,
        resolved_at=row.resolved_at,
        resolved_by=row.resolved_by,
        note=row.note,
    )


def _policy_row(row: Policy) -> PolicyRow:
    return PolicyRow(workspace_id=row.workspace_id, name=row.name, body=row.body, updated_at=row.updated_at)


@dataclass(frozen=True)
class ParsedWrite:
    """The fields a `benchpress-write/1` line carries, once its `action` sub-object is unpacked.

    Shared by `Store.append_receipt` (the gateway's own store) and `DiskReceiptSource` (`benchpress ui`'s
    read of a plain JSONL file), so a write line is parsed into a receipt summary exactly one way.
    """

    event: str
    at: str
    session: str
    provider: str
    method: str
    path: str
    status: str | None
    rule: str
    resource: str
    target_refs: tuple[str, ...]


def parse_write_line(payload: Mapping[str, Any]) -> ParsedWrite:
    """Parse an already-`json.loads`-ed `benchpress-write/1` line into its receipt-summary fields.

    `resource` is `target_refs[0]` when the action names any, else the action's `path` — the same rule
    `StoreReceiptSource` has always used.
    """
    action = cast(Mapping[str, Any], payload["action"])
    target_refs = tuple(str(ref) for ref in (action.get("target_refs") or ()))
    resource = target_refs[0] if target_refs else str(action["path"])
    return ParsedWrite(
        event=str(payload["event"]),
        at=str(payload["at"]),
        session=str(payload["session"]),
        provider=str(action["provider"]),
        method=str(action["method"]),
        path=str(action["path"]),
        status=cast("str | None", payload.get("status")),
        rule=str(cast(Mapping[str, Any], payload["verdict"])["rule"]),
        resource=resource,
        target_refs=target_refs,
    )


_ROW_ID = re.compile(r"[0-9]{1,19}")
_MAX_ROW_ID = 2**63 - 1


def _row_id(receipt_id: str) -> int | None:
    """A receipt id as its integer primary key, or None when no row can have it (not plain digits, or past 64 bits)."""
    if _ROW_ID.fullmatch(receipt_id) is None:
        return None
    value = int(receipt_id)
    return value if value <= _MAX_ROW_ID else None


class Store:
    """The gateway's durable state: workspaces, keys, sessions, receipts, write claims, approvals, policies."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @classmethod
    def open(cls, url: str, *, migrate: bool = True) -> Store:
        """Build the engine for `url` and, by default, migrate it to head before returning.

        Migration reuses this exact engine's own connection (rather than the module-level `upgrade(url)`,
        which opens a separate engine) so an in-memory SQLite database — whose schema lives only on one
        connection, kept alive by `make_engine`'s `StaticPool` — is migrated on the very connection this
        `Store` will keep using, instead of on a throwaway one that vanishes immediately after.
        """
        engine = make_engine(url)
        if migrate:
            with engine.connect() as connection:
                run_upgrade(connection, url, "head")
                connection.commit()
        return cls(engine)

    def ping(self) -> bool:
        try:
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return True
        except SQLAlchemyError:
            return False

    # ---- workspaces and keys --------------------------------------------------------

    def create_workspace(self, name: str) -> WorkspaceRow:
        workspace_id = f"ws_{secrets.token_hex(8)}"
        created_at = utc_now()
        with OrmSession(self.engine) as db, db.begin():
            db.add(Workspace(id=workspace_id, name=name, created_at=created_at))
        return WorkspaceRow(id=workspace_id, name=name, created_at=created_at)

    def workspace_by_name(self, name: str) -> WorkspaceRow | None:
        with OrmSession(self.engine) as db:
            row = db.execute(select(Workspace).where(Workspace.name == name)).scalar_one_or_none()
            return _workspace_row(row) if row is not None else None

    def workspace(self, id: str) -> WorkspaceRow | None:  # `id` matches the brief's interface verbatim
        with OrmSession(self.engine) as db:
            row = db.get(Workspace, id)
            return _workspace_row(row) if row is not None else None

    def workspaces(self) -> list[WorkspaceRow]:
        with OrmSession(self.engine) as db:
            return [_workspace_row(row) for row in db.execute(select(Workspace)).scalars()]

    def create_api_key(self, workspace_id: str, name: str, *, plaintext: str | None = None) -> tuple[ApiKeyRow, str]:
        secret = plaintext if plaintext is not None else f"bp_{secrets.token_urlsafe(32)}"
        key_id = f"key_{secrets.token_hex(8)}"
        created_at = utc_now()
        key_hash = hashlib.sha256(secret.encode()).hexdigest()
        prefix = secret[:10]
        with OrmSession(self.engine) as db, db.begin():
            db.add(
                ApiKey(
                    id=key_id,
                    workspace_id=workspace_id,
                    name=name,
                    prefix=prefix,
                    key_hash=key_hash,
                    created_at=created_at,
                    revoked_at=None,
                )
            )
        row = ApiKeyRow(id=key_id, workspace_id=workspace_id, name=name, prefix=prefix, created_at=created_at,
                        revoked_at=None)
        return row, secret

    def key_for(self, plaintext: str) -> tuple[WorkspaceRow, ApiKeyRow] | None:
        key_hash = hashlib.sha256(plaintext.encode()).hexdigest()
        with OrmSession(self.engine) as db:
            found = db.execute(
                select(ApiKey, Workspace)
                .join(Workspace, Workspace.id == ApiKey.workspace_id)
                .where(ApiKey.key_hash == key_hash, ApiKey.revoked_at.is_(None))
            ).first()
            if found is None:
                return None
            api_key, workspace = found
            return _workspace_row(workspace), _api_key_row(api_key)

    def revoke_api_key(self, key_id: str) -> None:
        with OrmSession(self.engine) as db, db.begin():
            db.execute(sa_update(ApiKey).where(ApiKey.id == key_id).values(revoked_at=utc_now()))

    # ---- sessions ---------------------------------------------------------------------

    def create_session(
        self,
        workspace_id: str,
        context: Context,
        *,
        session_id: str | None = None,
        idempotency_scope: Literal["session", "workspace"] = "session",
    ) -> str:
        sid = session_id if session_id is not None else f"ses_{secrets.token_hex(16)}"
        created_at = utc_now()
        with OrmSession(self.engine) as db:
            try:
                with db.begin():
                    db.add(
                        SessionModel(
                            id=sid,
                            workspace_id=workspace_id,
                            context=context.model_dump_json(),
                            idempotency_scope=idempotency_scope,
                            created_at=created_at,
                        )
                    )
            except IntegrityError as exc:
                raise SessionExists(f"session {sid!r} already exists in workspace {workspace_id!r}") from exc
        return sid

    def load_session(self, workspace_id: str, session_id: str) -> tuple[Context, str] | None:
        with OrmSession(self.engine) as db:
            row = db.get(SessionModel, (workspace_id, session_id))
            if row is None:
                return None
            return Context.model_validate_json(row.context), row.idempotency_scope

    # ---- receipts -----------------------------------------------------------------------

    def append_receipt(self, workspace_id: str, session_id: str, line: str) -> ReceiptRow:
        parsed = parse_write_line(json.loads(line))
        with OrmSession(self.engine) as db, db.begin():
            row = Receipt(
                workspace_id=workspace_id,
                session_id=session_id,
                event=parsed.event,
                at=parsed.at,
                provider=parsed.provider,
                method=parsed.method,
                path=parsed.path,
                status=parsed.status,
                rule=parsed.rule,
                resource=parsed.resource,
                refs="\n".join(parsed.target_refs),
                line=line,
            )
            db.add(row)
            db.flush()
            return _receipt_row(row)

    def receipts(
        self,
        workspace_id: str,
        *,
        customer: str | None = None,
        provider: str | None = None,
        rule: str | None = None,
        status: str | None = None,
        session: str | None = None,
        event: str | None = None,
        limit: int = 100,
        before: str | None = None,
    ) -> list[ReceiptRow]:
        stmt = select(Receipt).where(Receipt.workspace_id == workspace_id)
        if provider is not None:
            stmt = stmt.where(Receipt.provider == provider)
        if rule is not None:
            stmt = stmt.where(Receipt.rule == rule)
        if status is not None:
            stmt = stmt.where(Receipt.status == status)
        if session is not None:
            stmt = stmt.where(Receipt.session_id == session)
        if event is not None:
            stmt = stmt.where(Receipt.event == event)
        if customer is not None:
            term = f"%{customer}%"
            stmt = stmt.where(or_(Receipt.resource.ilike(term), Receipt.refs.ilike(term)))
        if before is not None:
            cursor = _row_id(before)
            if cursor is None:
                return []  # not a cursor this store issued
            stmt = stmt.where(Receipt.id < cursor)
        stmt = stmt.order_by(Receipt.id.desc()).limit(limit)
        with OrmSession(self.engine) as db:
            try:
                return [_receipt_row(row) for row in db.execute(stmt).scalars()]
            except DataError:
                if before is None:
                    raise
                return []  # a cursor the id column cannot hold (Postgres INTEGER) is not one this store issued

    def receipt(self, workspace_id: str, receipt_id: str) -> ReceiptRow | None:
        row_id = _row_id(receipt_id)
        if row_id is None:
            return None
        with OrmSession(self.engine) as db:
            try:
                row = db.get(Receipt, row_id)
            except DataError:  # past the id column's range (Postgres INTEGER): no such receipt
                return None
            if row is None or row.workspace_id != workspace_id:
                return None
            return _receipt_row(row)

    # ---- idempotency (write claims) ------------------------------------------------------

    def claim_write(
        self, scope: str, fingerprint: str, *, holder: str, now: float, lease_seconds: float
    ) -> ClaimResult:
        with OrmSession(self.engine) as db, db.begin():
            try:
                with db.begin_nested():
                    db.execute(
                        insert(Write).values(
                            scope=scope, fingerprint=fingerprint, state="in_flight", holder=holder,
                            claimed_at=now, completed_at=None,
                        )
                    )
                return "claimed"
            except IntegrityError:
                pass  # the nested transaction already rolled back to the savepoint; read the row instead
            row = db.execute(select(Write).where(Write.scope == scope, Write.fingerprint == fingerprint)).scalar_one()
            if row.state == "done":
                return "done"
            if row.state == "in_flight" and now - row.claimed_at < lease_seconds:
                return "in_flight"
            result = cast(
                CursorResult[Any],
                db.execute(
                    sa_update(Write)
                    .where(
                        Write.scope == scope,
                        Write.fingerprint == fingerprint,
                        Write.state == "in_flight",
                        Write.claimed_at == row.claimed_at,
                    )
                    .values(claimed_at=now, holder=holder)
                ),
            )
            return "claimed" if result.rowcount == 1 else "in_flight"

    def complete_write(self, scope: str, fingerprint: str, *, holder: str, succeeded: bool, now: float) -> None:
        with OrmSession(self.engine) as db, db.begin():
            if succeeded:
                # The write happened: go done unconditionally, whoever holds the lease now. The row may
                # already be gone — a lease takeover's original (stale) holder can complete(succeeded=True)
                # after its own successor already completed(succeeded=False) and deleted the row (Ruling
                # R4's release path). An UPDATE alone would then silently lose the success, so upsert: if
                # the UPDATE matches nothing, insert a fresh `done` row, falling back to a retry of the
                # UPDATE if a concurrent writer's insert lands first (Postgres row-level concurrency; SQLite
                # never actually takes this branch since the whole transaction is serialized).
                result = cast(
                    CursorResult[Any],
                    db.execute(
                        sa_update(Write).where(Write.scope == scope, Write.fingerprint == fingerprint)
                        .values(state="done", completed_at=now)
                    ),
                )
                if result.rowcount == 0:
                    try:
                        with db.begin_nested():
                            db.execute(
                                insert(Write).values(
                                    scope=scope, fingerprint=fingerprint, state="done", holder=holder,
                                    claimed_at=now, completed_at=now,
                                )
                            )
                    except IntegrityError:
                        db.execute(
                            sa_update(Write).where(Write.scope == scope, Write.fingerprint == fingerprint)
                            .values(state="done", completed_at=now)
                        )
            else:
                # A release only clears the entry this holder itself owns (Ruling R4): a stale holder
                # must never clobber a newer holder's claim after a lease takeover.
                db.execute(
                    delete(Write).where(
                        Write.scope == scope,
                        Write.fingerprint == fingerprint,
                        Write.state == "in_flight",
                        Write.holder == holder,
                    )
                )

    # ---- approvals ------------------------------------------------------------------------

    def insert_approval(self, row: ApprovalRow) -> None:
        with OrmSession(self.engine) as db, db.begin():
            db.add(
                Approval(
                    id=row.id,
                    workspace_id=row.workspace_id,
                    session_id=row.session_id,
                    action=row.action.model_dump_json(),
                    fingerprint=row.fingerprint,
                    rule_name=row.rule_name,
                    status=row.status,
                    requested_at=row.requested_at,
                    expires_at=row.expires_at,
                    resolved_at=row.resolved_at,
                    resolved_by=row.resolved_by,
                    note=row.note,
                )
            )

    def approval(self, workspace_id: str, approval_id: str) -> ApprovalRow | None:
        with OrmSession(self.engine) as db:
            row = db.get(Approval, approval_id)
            if row is None or row.workspace_id != workspace_id:
                return None
            return _approval_row(row)

    def approvals(self, workspace_id: str, *, status: str | None = None) -> list[ApprovalRow]:
        stmt = select(Approval).where(Approval.workspace_id == workspace_id)
        if status is not None:
            stmt = stmt.where(Approval.status == status)
        stmt = stmt.order_by(Approval.requested_at, Approval.id)
        with OrmSession(self.engine) as db:
            return [_approval_row(row) for row in db.execute(stmt).scalars()]

    def pending_approval_for(self, workspace_id: str, session_id: str, fingerprint: str) -> ApprovalRow | None:
        with OrmSession(self.engine) as db:
            row = db.execute(
                select(Approval).where(
                    Approval.workspace_id == workspace_id,
                    Approval.session_id == session_id,
                    Approval.fingerprint == fingerprint,
                    Approval.status == "pending",
                )
            ).scalars().first()
            return _approval_row(row) if row is not None else None

    def update_approval(
        self,
        approval_id: str,
        *,
        status: str,
        resolved_at: str | None,
        resolved_by: str | None,
        note: str,
        expected_status: str = "pending",
    ) -> bool:
        with OrmSession(self.engine) as db, db.begin():
            result = cast(
                CursorResult[Any],
                db.execute(
                    sa_update(Approval)
                    .where(Approval.id == approval_id, Approval.status == expected_status)
                    .values(status=status, resolved_at=resolved_at, resolved_by=resolved_by, note=note)
                ),
            )
            return result.rowcount == 1

    # ---- policies -------------------------------------------------------------------------

    def put_policy(self, workspace_id: str, name: str, body: str) -> PolicyRow:
        updated_at = utc_now()
        with OrmSession(self.engine) as db, db.begin():
            row = db.get(Policy, (workspace_id, name))
            if row is None:
                db.add(Policy(workspace_id=workspace_id, name=name, body=body, updated_at=updated_at))
            else:
                row.body = body
                row.updated_at = updated_at
        return PolicyRow(workspace_id=workspace_id, name=name, body=body, updated_at=updated_at)

    def policies(self, workspace_id: str) -> list[PolicyRow]:
        with OrmSession(self.engine) as db:
            stmt = select(Policy).where(Policy.workspace_id == workspace_id).order_by(Policy.name)
            return [_policy_row(row) for row in db.execute(stmt).scalars()]

    def delete_policy(self, workspace_id: str, name: str) -> bool:
        with OrmSession(self.engine) as db, db.begin():
            result = cast(
                CursorResult[Any],
                db.execute(delete(Policy).where(Policy.workspace_id == workspace_id, Policy.name == name)),
            )
            return result.rowcount > 0


@dataclass
class SqlIdempotencyStore:
    """Adapts `Store.claim_write`/`complete_write` to the `IdempotencyStore` protocol.

    Both calls are synchronous SQL under the hood; `anyio.to_thread.run_sync` keeps them off the event
    loop so a slow gateway replica's database round trip never blocks other in-flight requests.
    """

    store: Store
    lease_seconds: float = 300.0
    now: Callable[[], float] = time.time

    async def claim(self, scope: str, fingerprint: str, *, holder: str) -> ClaimResult:
        call = functools.partial(
            self.store.claim_write, scope, fingerprint, holder=holder, now=self.now(), lease_seconds=self.lease_seconds
        )
        return await anyio.to_thread.run_sync(call)

    async def complete(self, scope: str, fingerprint: str, *, holder: str, succeeded: bool) -> None:
        call = functools.partial(
            self.store.complete_write, scope, fingerprint, holder=holder, succeeded=succeeded, now=self.now()
        )
        await anyio.to_thread.run_sync(call)
