"""Shared FastAPI dependencies for the gateway's routes: the service, auth, and receipt sources.

Every route authenticates through `WorkspaceDep`, fetches the service through `ServiceDep`, and returns its
model; the behaviour lives in `GatewayService`, not here.
"""

from __future__ import annotations

from typing import Annotated, cast

import anyio.to_thread
from fastapi import Depends, HTTPException, Request
from fastapi import Path as PathParam

from benchpress.gateway.auth import RateLimiter, bearer_token
from benchpress.gateway.receipt_sources import ReceiptSource, StoreReceiptSource
from benchpress.gateway.service import GatewayService
from benchpress.gateway.store import ApiKeyRow, WorkspaceRow

__all__ = [
    "PolicyName",
    "ReceiptSourceDep",
    "ServiceDep",
    "WorkspaceDep",
    "actor",
    "authenticate",
    "current_workspace",
]


def _service(request: Request) -> GatewayService:
    return cast(GatewayService, request.app.state.service)


async def authenticate(request: Request, service: GatewayService) -> tuple[WorkspaceRow, ApiKeyRow]:
    """A valid bearer key and its workspace (401 without one), then the per-key rate limit (429 over it).

    Shared by `current_workspace` (gated on `settings.auth`) and the `/metrics` route (gated on
    `settings.metrics_auth`), so both enforce the identical key lookup and rate limit, never two copies of it.
    """
    token = bearer_token(request.headers.get("authorization"))
    found = await anyio.to_thread.run_sync(service.store.key_for, token) if token is not None else None
    if found is None:
        raise HTTPException(401, "a valid API key is required", headers={"WWW-Authenticate": "Bearer"})
    workspace, key = found
    if not cast(RateLimiter, request.app.state.limiter).allow(key.id):
        raise HTTPException(429, "rate limit exceeded", headers={"Retry-After": "1"})
    return workspace, key


async def current_workspace(request: Request) -> WorkspaceRow:
    """The caller's workspace. The API key row is kept on `request.state.key` (None under `auth = "none"`)."""
    service = _service(request)
    if service.settings.auth == "none":
        request.state.key = None
        return await service.default_workspace()
    workspace, key = await authenticate(request, service)
    request.state.key = key
    return workspace


ServiceDep = Annotated[GatewayService, Depends(_service)]
WorkspaceDep = Annotated[WorkspaceRow, Depends(current_workspace)]


async def _receipt_source(request: Request, workspace: WorkspaceDep) -> ReceiptSource:
    return StoreReceiptSource(_service(request).store, workspace)


ReceiptSourceDep = Annotated[ReceiptSource, Depends(_receipt_source)]
PolicyName = Annotated[str, PathParam(max_length=64)]


def actor(request: Request) -> str:
    """The caller's identity for an approval decision: the API key's name, or `anonymous` under `auth = "none"`."""
    key: ApiKeyRow | None = request.state.key
    return key.name if key is not None else "anonymous"
