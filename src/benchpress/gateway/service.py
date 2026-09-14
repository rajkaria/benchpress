"""`GatewayService`: the operations behind every gateway surface, the HTTP routes and the MCP tools alike.

A surface authenticates, calls one method here and returns its result. `RequestRejected.status` is the only
HTTP-shaped thing in this module; each surface maps it to its own error.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, cast

import anyio.to_thread
from sqlalchemy.exc import IntegrityError

from benchpress.context import Action
from benchpress.gate import PolicyRuleSet
from benchpress.gateway.config import ConfigError, Settings
from benchpress.gateway.schemas import ContextInput, ExecuteRequest, ExecuteResponse, SessionCreate, SessionCreated
from benchpress.gateway.store import Store, WorkspaceRow
from benchpress.packs import (
    PolicyPack,
    PolicyPackError,
    available_policy_packs,
    load_policy_packs,
    policy_pack_from_yaml,
)
from benchpress.tools import PROVIDER_API, interpret_result
from benchpress.write_receipts import Clock, write_line

if TYPE_CHECKING:
    from benchpress.gateway.sessions import GatewaySession, SessionRegistry

__all__ = ["DEFAULT_WORKSPACE", "GatewayService", "RequestRejected", "load_directory_packs", "workspace_packs"]

DEFAULT_WORKSPACE = "default"


class RequestRejected(ValueError):
    """A request refused before anything runs, carrying the HTTP-style status every surface reports."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def load_directory_packs(policy_dir: Path | None) -> tuple[PolicyPack, ...]:
    """Every `*.yaml` pack in `policy_dir`, by file name. A missing directory or a bad pack is a `ConfigError`."""
    if policy_dir is None:
        return ()
    if not policy_dir.is_dir():
        raise ConfigError(f"policy_dir {str(policy_dir)!r} is not a directory")
    try:
        return load_policy_packs(sorted(policy_dir.glob("*.yaml")))
    except PolicyPackError as exc:
        raise ConfigError(f"policy_dir: {exc}") from exc


def workspace_packs(store: Store, directory: Sequence[PolicyPack], workspace_id: str) -> tuple[PolicyRuleSet, ...]:
    """The directory packs, then the workspace's stored packs parsed now. Reads the store: call it in a thread."""
    stored = [policy_pack_from_yaml(row.body, source=f"workspace:{row.name}") for row in store.policies(workspace_id)]
    return (*directory, *stored)


class GatewayService:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        sessions: SessionRegistry,
        *,
        clock: Clock,
        now: Callable[[], float],
        directory_packs: Sequence[PolicyPack] = (),
    ) -> None:
        self.settings = settings
        self.store = store
        self.sessions = sessions
        self._clock = clock
        self._now = now
        self._directory_packs = tuple(directory_packs)
        self._default_workspace: WorkspaceRow | None = None

    # ---- workspaces and health ------------------------------------------------------------

    async def default_workspace(self) -> WorkspaceRow:
        """The workspace every request acts as under `auth = "none"`, created on first use."""
        if self._default_workspace is None:
            self._default_workspace = await anyio.to_thread.run_sync(self._ensure_default_workspace)
        return self._default_workspace

    def _ensure_default_workspace(self) -> WorkspaceRow:
        found = self.store.workspace_by_name(DEFAULT_WORKSPACE)
        if found is not None:
            return found
        try:
            return self.store.create_workspace(DEFAULT_WORKSPACE)
        except IntegrityError:
            created = self.store.workspace_by_name(DEFAULT_WORKSPACE)  # another replica created it first
            if created is None:
                raise
            return created

    async def store_healthy(self) -> bool:
        return await anyio.to_thread.run_sync(self.store.ping)

    # ---- sessions, writes and reads ----------------------------------------------------------

    async def create_session(self, workspace: WorkspaceRow, body: SessionCreate) -> SessionCreated:
        return await self.sessions.create(workspace, body)

    async def execute(self, workspace: WorkspaceRow, request: ExecuteRequest) -> ExecuteResponse:
        """Gate, execute and read back one write, then append its one receipt line."""
        if request.context is not None:
            # An inline context leaves the caller no session id to replay under, so its writes share the workspace's
            # claims: resending the same request is refused as a duplicate instead of being written again.
            inline = SessionCreate(context=request.context, idempotency_scope="workspace")
            session_id = (await self.sessions.create(workspace, inline)).session_id
        else:
            session_id = cast(str, request.session_id)
        session = await self._session(workspace, session_id)
        outcome = await session.writer.run(request.action)
        line = write_line(outcome, at=self._clock(), workspace=workspace.name, session=session_id)
        row = await anyio.to_thread.run_sync(self.store.append_receipt, workspace.id, session_id, line)
        return ExecuteResponse(
            status=outcome.status,
            session_id=session_id,
            verdict=outcome.verdict,
            status_code=outcome.result.status_code if outcome.result is not None else None,
            evidence=list(outcome.evidence),
            receipt_id=row.id,
        )

    async def explain(
        self, workspace: WorkspaceRow, action: Action, *, session_id: str | None, context: ContextInput | None
    ) -> dict[str, object]:
        """The gate's verdict on `action`, without executing, claiming or receipting anything.

        An inline `context` is judged by an unpersisted writer, so explaining never creates a session.
        """
        if (session_id is None) == (context is None):
            raise RequestRejected(422, "send exactly one of session_id or context")
        if context is not None:
            writer = await self.sessions.unpersisted(workspace, context)
        else:
            writer = (await self._session(workspace, cast(str, session_id))).writer
        verdict = writer.evaluate(action)
        return {"allowed": verdict.allowed, "rule": verdict.rule, "reason": verdict.reason}

    async def read(
        self, workspace: WorkspaceRow, provider: str, path: str, query: Mapping[str, str]
    ) -> dict[str, object]:
        """One provider GET through the executor, interpreted like every tool-bus response. Reads are never gated."""
        tool_input = {"provider": provider, "method": "GET", "path": path, "query": dict(query)}
        result = interpret_result(await self.sessions.executor(PROVIDER_API, tool_input))
        return {"ok": result.ok, "status_code": result.status_code, "body": result.body, "error": result.error}

    async def _session(self, workspace: WorkspaceRow, session_id: str) -> GatewaySession:
        session = await self.sessions.get(workspace, session_id)
        if session is None:
            raise RequestRejected(404, f"no session {session_id!r} in this workspace")
        return session

    # ---- policy packs ------------------------------------------------------------------------

    async def policies(self, workspace: WorkspaceRow) -> dict[str, object]:
        rows = await anyio.to_thread.run_sync(self.store.policies, workspace.id)
        return {
            "bundled": list(available_policy_packs()),
            "directory": [pack.name for pack in self._directory_packs],
            "workspace": [{"name": row.name, "updated_at": row.updated_at} for row in rows],
        }

    async def put_policy(self, workspace: WorkspaceRow, name: str, text: str) -> dict[str, str]:
        """Store a pack for the workspace. Sessions built from now on enforce it."""
        try:
            pack = policy_pack_from_yaml(text, source=f"workspace:{name}")
        except PolicyPackError as exc:
            raise RequestRejected(422, str(exc)) from exc
        if pack.name != name:
            raise RequestRejected(422, f"the pack is named {pack.name!r}, so it belongs at /v1/policies/{pack.name}")
        row = await anyio.to_thread.run_sync(self.store.put_policy, workspace.id, name, text)
        self.sessions.invalidate(workspace.id)
        return {"name": row.name, "updated_at": row.updated_at}

    async def delete_policy(self, workspace: WorkspaceRow, name: str) -> None:
        if not await anyio.to_thread.run_sync(self.store.delete_policy, workspace.id, name):
            raise RequestRejected(404, f"no policy {name!r} in this workspace")
        self.sessions.invalidate(workspace.id)
