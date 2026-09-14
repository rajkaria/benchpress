"""Gateway sessions: a persisted context, and the `VerifiedWrite` each replica builds from it.

A session is a row, so any replica can rebuild it; the registry only caches what it built, most recently used last.
Write claims never live in the cache: every session's writer shares the store-backed claims, so a replay is refused
whichever replica receives it.
"""

from __future__ import annotations

import functools
import secrets
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import anyio.to_thread

from benchpress.gate import PolicyRuleSet
from benchpress.gateway.schemas import ContextInput, SessionCreate, SessionCreated
from benchpress.gateway.service import RequestRejected
from benchpress.gateway.store import SessionExists, Store, WorkspaceRow
from benchpress.idempotency import IdempotencyStore
from benchpress.tools import ToolExecutor
from benchpress.verified import VerifiedWrite

__all__ = ["GatewaySession", "SessionNotFound", "SessionRegistry"]


class SessionNotFound(LookupError):
    """No session with this id in the caller's workspace. Another workspace's session ids are not found either."""


@dataclass
class GatewaySession:
    workspace: WorkspaceRow
    session_id: str
    scope_key: str
    writer: VerifiedWrite


class SessionRegistry:
    """Creates session rows and builds (and caches, per replica) the writer for each."""

    def __init__(
        self,
        store: Store,
        executor: ToolExecutor,
        *,
        claims: IdempotencyStore,
        packs_for: Callable[[str], Sequence[PolicyRuleSet]],
        cache_size: int = 1024,
    ) -> None:
        self.store = store
        self.executor = executor
        self._claims = claims
        self._packs_for = packs_for
        self._cache_size = cache_size
        self._cache: OrderedDict[tuple[str, str], GatewaySession] = OrderedDict()
        self._generations: dict[str, int] = {}

    async def create(self, workspace: WorkspaceRow, body: SessionCreate) -> SessionCreated:
        """Persist a session. Without a `session_id` one is generated (`ses_` + 16 hex); a taken id is a 409."""
        session_id = body.session_id if body.session_id is not None else f"ses_{secrets.token_hex(8)}"
        create = functools.partial(
            self.store.create_session,
            workspace.id,
            body.context.to_context(session_id),
            session_id=session_id,
            idempotency_scope=body.idempotency_scope,
        )
        try:
            await anyio.to_thread.run_sync(create)
        except SessionExists as exc:
            raise RequestRejected(409, f"session {session_id!r} already exists in this workspace") from exc
        return SessionCreated(session_id=session_id, idempotency_scope=body.idempotency_scope)

    async def get(self, workspace: WorkspaceRow, session_id: str) -> GatewaySession | None:
        """The session, cached or rebuilt from its row; None when this workspace has no such session."""
        key = (workspace.id, session_id)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached
        generation = self._generations.get(workspace.id, 0)
        loaded = await anyio.to_thread.run_sync(self.store.load_session, workspace.id, session_id)
        if loaded is None:
            return None
        context, scope = loaded
        scope_key = workspace.id if scope == "workspace" else f"{workspace.id}:{session_id}"
        packs = await anyio.to_thread.run_sync(self._packs_for, workspace.id)
        writer = VerifiedWrite(
            self.executor,
            context=context,
            policy_packs=tuple(packs),
            allow_unplanned=True,
            idempotency=self._claims,
            scope=scope_key,
            workspace=workspace.name,
            session=session_id,
        )
        built = GatewaySession(workspace, session_id, scope_key, writer)
        if self._generations.get(workspace.id, 0) != generation:
            return built  # the workspace's packs changed while this was built: serve this request, cache nothing
        # Two requests can rebuild one row at once. Both writers share the claims; the first one cached is kept.
        session = self._cache.setdefault(key, built)
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return session

    async def unpersisted(self, workspace: WorkspaceRow, context: ContextInput) -> VerifiedWrite:
        """A writer over an inline context, for judging an action without storing a session or sharing claims."""
        packs = await anyio.to_thread.run_sync(self._packs_for, workspace.id)
        return VerifiedWrite(
            self.executor,
            context=context.to_context("inline"),
            policy_packs=tuple(packs),
            allow_unplanned=True,
            workspace=workspace.name,
        )

    def invalidate(self, workspace_id: str) -> None:
        """Drop this replica's cached sessions for a workspace, so the next request rebuilds them with current packs."""
        self._generations[workspace_id] = self._generations.get(workspace_id, 0) + 1
        for key in [key for key in self._cache if key[0] == workspace_id]:
            del self._cache[key]
