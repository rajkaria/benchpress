"""Shared FastAPI dependencies for the gateway's routes: the service and auth.

Every route authenticates through `WorkspaceDep`, fetches the service through `ServiceDep`, and returns its
model; the behaviour lives in `GatewayService`, not here. `gateway.app.receipts_router` calls
`current_workspace` directly to build its own `ReceiptSource` dependency.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, cast

import anyio.to_thread
from fastapi import Depends, HTTPException, Request
from fastapi import Path as PathParam
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from benchpress.gateway.auth import RateLimiter, bearer_token
from benchpress.gateway.service import GatewayService
from benchpress.gateway.store import ApiKeyRow, WorkspaceRow

__all__ = [
    "KeyRefused",
    "PolicyName",
    "RequireKey",
    "ServiceDep",
    "WorkspaceDep",
    "actor",
    "authenticate",
    "charge_rate_limit",
    "check_key",
    "current_workspace",
    "header_authenticator",
    "lookup_key",
]


def _service(request: Request) -> GatewayService:
    return cast(GatewayService, request.app.state.service)


class KeyRefused(PermissionError):
    """A missing, unknown or rate-limited API key. `status` (401 or 429) and `headers` are what HTTP answers with."""

    def __init__(self, status: int, message: str, headers: dict[str, str]) -> None:
        super().__init__(message)
        self.status = status
        self.headers = headers


async def lookup_key(header: str | None, service: GatewayService) -> tuple[WorkspaceRow, ApiKeyRow]:
    """The gateway's one key lookup: the valid bearer key in `header` and its workspace, else `KeyRefused` (401)."""
    token = bearer_token(header)
    found = await anyio.to_thread.run_sync(service.store.key_for, token) if token is not None else None
    if found is None:
        raise KeyRefused(401, "a valid API key is required", {"WWW-Authenticate": "Bearer"})
    return found


def charge_rate_limit(key: ApiKeyRow, limiter: RateLimiter) -> None:
    """Spend one of `key`'s requests from `limiter`, else `KeyRefused` (429)."""
    if not limiter.allow(key.id):
        raise KeyRefused(429, "rate limit exceeded", {"Retry-After": "1"})


async def check_key(
    header: str | None, service: GatewayService, limiter: RateLimiter
) -> tuple[WorkspaceRow, ApiKeyRow]:
    """The gateway's one key check: `lookup_key`, then `charge_rate_limit`. Raises `KeyRefused`.

    Every authenticated surface goes through here (`authenticate` for the HTTP routes and `/metrics`,
    `header_authenticator` for the MCP tools), so all of them enforce the identical lookup, and passing them the
    app's one `app.state.limiter` gives each key a single budget across surfaces.
    """
    workspace, key = await lookup_key(header, service)
    charge_rate_limit(key, limiter)
    return workspace, key


class RequireKey:
    """Pure ASGI: under `auth = "api_key"`, answers 401 before `app` sees a request without a valid key.

    Wraps the mounted MCP app, so an anonymous caller can never open an MCP session. It only looks the key up and
    never charges the rate limit: one streamable-HTTP tool call is several HTTP requests, and the tool itself
    charges the key's one shared budget through `check_key`. Under `auth = "none"` (loopback only) it lets
    everything through.
    """

    def __init__(self, app: ASGIApp, service: GatewayService) -> None:
        self.app = app
        self.service = service

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and self.service.settings.auth != "none":
            try:
                await lookup_key(Headers(scope=scope).get("authorization"), self.service)
            except KeyRefused as refused:
                response = JSONResponse({"detail": str(refused)}, status_code=refused.status, headers=refused.headers)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


async def authenticate(request: Request, service: GatewayService) -> tuple[WorkspaceRow, ApiKeyRow]:
    """`check_key` on the request's `Authorization` header against `app.state.limiter`, refusing with 401 or 429.

    Shared by `current_workspace` (gated on `settings.auth`) and the `/metrics` route (gated on
    `settings.metrics_auth`), so both enforce the identical key lookup and rate limit, never two copies of it.
    """
    limiter = cast(RateLimiter, request.app.state.limiter)
    try:
        return await check_key(request.headers.get("authorization"), service, limiter)
    except KeyRefused as refused:
        raise HTTPException(refused.status, str(refused), headers=refused.headers) from None


def header_authenticator(
    service: GatewayService, limiter: RateLimiter
) -> Callable[[str | None], Awaitable[WorkspaceRow]]:
    """The MCP tools' `Authenticator`, applying `current_workspace`'s rules to a raw `Authorization` header value.

    Under `auth = "none"` every caller acts as the default workspace. Otherwise the header must pass `check_key`
    against `limiter`, which must be the app's `app.state.limiter`. A refusal raises `KeyRefused`, a
    `PermissionError`, whose message never contains the key.
    """

    async def authenticate_header(header: str | None) -> WorkspaceRow:
        if service.settings.auth == "none":
            return await service.default_workspace()
        workspace, _key = await check_key(header, service, limiter)
        return workspace

    return authenticate_header


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
PolicyName = Annotated[str, PathParam(max_length=64)]


def actor(request: Request) -> str:
    """The caller's identity for an approval decision: the API key's name, or `anonymous` under `auth = "none"`."""
    key: ApiKeyRow | None = request.state.key
    return key.name if key is not None else "anonymous"
