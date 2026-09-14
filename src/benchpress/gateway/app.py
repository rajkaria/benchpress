"""`create_app`: the Benchpress gateway as a FastAPI application.

Each route authenticates, calls `GatewayService` (or a `ReceiptSource`) and returns its model; the behaviour lives
in the service. The body cap is a pure ASGI middleware, so it holds whatever a route reads and however it reads it.
"""

from __future__ import annotations

import functools
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import Annotated, cast

import anyio.to_thread
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from benchpress import __version__
from benchpress.context import utc_now
from benchpress.gateway.auth import RateLimiter, bearer_token
from benchpress.gateway.config import Settings
from benchpress.gateway.executors import executor_from_settings
from benchpress.gateway.receipt_sources import ReceiptSource, StoreReceiptSource
from benchpress.gateway.schemas import (
    ExecuteRequest,
    ExecuteResponse,
    ReceiptDetail,
    ReceiptFilters,
    ReceiptPage,
    SessionCreate,
    SessionCreated,
)
from benchpress.gateway.service import GatewayService, RequestRejected, load_directory_packs, workspace_packs
from benchpress.gateway.sessions import SessionRegistry
from benchpress.gateway.store import SqlIdempotencyStore, Store, WorkspaceRow
from benchpress.tools import ToolExecutor
from benchpress.write_receipts import Clock

__all__ = ["BodySizeLimit", "create_app", "current_workspace"]


# ---- body cap ---------------------------------------------------------------------------------


class _BodyTooLarge(Exception):
    """Raised from `receive` once the streamed body passes the cap."""


async def _too_large(scope: Scope, receive: Receive, send: Send) -> None:
    await JSONResponse({"detail": "request body too large"}, status_code=413)(scope, receive, send)


class BodySizeLimit:
    """Caps request bodies at `max_bytes`: by `Content-Length` before reading, then by counting the bytes received."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = Headers(scope=scope).get("content-length", "")
        if declared.isascii() and declared.isdigit() and int(declared) > self.max_bytes:
            await _too_large(scope, receive, send)
            return
        received = 0
        exceeded = responded = False

        async def counting_receive() -> Message:
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    exceeded = True
                    raise _BodyTooLarge
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal responded
            if exceeded:  # whatever the app made of the aborted read, the client is told the body was too large
                if message["type"] == "http.response.start" and not responded:
                    responded = True
                    await _too_large(scope, receive, send)
                return
            responded = responded or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, counting_receive, guarded_send)
        except Exception:
            if not exceeded:
                raise
            if not responded:
                await _too_large(scope, receive, send)


# ---- dependencies -----------------------------------------------------------------------------


def _service(request: Request) -> GatewayService:
    return cast(GatewayService, request.app.state.service)


async def current_workspace(request: Request) -> WorkspaceRow:
    """The caller's workspace. The API key row is kept on `request.state.key` (None under `auth = "none"`)."""
    service = _service(request)
    if service.settings.auth == "none":
        request.state.key = None
        return await service.default_workspace()
    token = bearer_token(request.headers.get("authorization"))
    found = await anyio.to_thread.run_sync(service.store.key_for, token) if token is not None else None
    if found is None:
        raise HTTPException(401, "a valid API key is required", headers={"WWW-Authenticate": "Bearer"})
    workspace, key = found
    if not cast(RateLimiter, request.app.state.limiter).allow(key.id):
        raise HTTPException(429, "rate limit exceeded", headers={"Retry-After": "1"})
    request.state.key = key
    return workspace


ServiceDep = Annotated[GatewayService, Depends(_service)]
WorkspaceDep = Annotated[WorkspaceRow, Depends(current_workspace)]


async def _receipt_source(request: Request, workspace: WorkspaceDep) -> ReceiptSource:
    return StoreReceiptSource(_service(request).store, workspace)


ReceiptSourceDep = Annotated[ReceiptSource, Depends(_receipt_source)]
PolicyName = Annotated[str, PathParam(max_length=64)]


async def _rejected(_request: Request, exc: Exception) -> Response:
    rejected = cast(RequestRejected, exc)
    return JSONResponse(status_code=rejected.status, content={"detail": str(rejected)})


# ---- routes -----------------------------------------------------------------------------------

router = APIRouter()


@router.get("/healthz")
async def healthz(service: ServiceDep) -> JSONResponse:
    state = "ok" if await service.store_healthy() else "error"
    body = {"status": state, "version": __version__, "store": state}
    return JSONResponse(body, status_code=200 if state == "ok" else 503)


@router.get("/v1/meta")
async def meta(service: ServiceDep, _workspace: WorkspaceDep) -> dict[str, str]:
    return {"mode": "gateway", "version": __version__, "auth": service.settings.auth}


@router.post("/v1/sessions", status_code=201)
async def create_session(body: SessionCreate, workspace: WorkspaceDep, service: ServiceDep) -> SessionCreated:
    return await service.create_session(workspace, body)


@router.post("/v1/execute")
async def execute(body: ExecuteRequest, workspace: WorkspaceDep, service: ServiceDep) -> ExecuteResponse:
    return await service.execute(workspace, body)


@router.get("/v1/receipts")
async def list_receipts(filters: Annotated[ReceiptFilters, Query()], source: ReceiptSourceDep) -> ReceiptPage:
    return await source.list(filters)


@router.get("/v1/receipts/{receipt_id}")
async def get_receipt(receipt_id: str, source: ReceiptSourceDep) -> ReceiptDetail:
    detail = await source.get(receipt_id)
    if detail is None:
        raise HTTPException(404, "no such receipt in this workspace")
    return detail


@router.get("/v1/receipts/{receipt_id}/html", response_class=HTMLResponse)
async def get_receipt_html(receipt_id: str, source: ReceiptSourceDep) -> HTMLResponse:
    page = await source.html(receipt_id)
    if page is None:
        raise HTTPException(404, "no HTML page for this receipt")
    return HTMLResponse(page)


@router.get("/v1/policies")
async def list_policies(workspace: WorkspaceDep, service: ServiceDep) -> dict[str, object]:
    return await service.policies(workspace)


@router.put("/v1/policies/{name}")
async def put_policy(
    name: PolicyName, request: Request, workspace: WorkspaceDep, service: ServiceDep
) -> dict[str, str]:
    try:
        text = (await request.body()).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RequestRejected(422, "a policy pack is UTF-8 YAML") from exc
    return await service.put_policy(workspace, name, text)


@router.delete("/v1/policies/{name}", status_code=204)
async def delete_policy(name: PolicyName, workspace: WorkspaceDep, service: ServiceDep) -> None:
    await service.delete_policy(workspace, name)


# ---- the app ----------------------------------------------------------------------------------


async def _nothing_to_close() -> None:
    return None


def create_app(
    settings: Settings,
    *,
    store: Store | None = None,
    executor: ToolExecutor | None = None,
    clock: Clock = utc_now,
    now: Callable[[], float] = time.time,
) -> FastAPI:
    """Build the gateway. `app.state.service` and `app.state.store` are set here, before the lifespan runs.

    Raises `ConfigError` for a bad `policy_dir` pack or upstream, before opening anything that would need closing.
    """
    directory_packs = load_directory_packs(settings.policy_dir)
    if executor is None:
        executor, close_executor = executor_from_settings(settings)
    else:
        close_executor = _nothing_to_close
    owns_store = store is None
    opened = Store.open(settings.store) if store is None else store

    claims = SqlIdempotencyStore(opened, lease_seconds=settings.idempotency_lease_seconds, now=now)
    sessions = SessionRegistry(
        opened,
        executor,
        claims=claims,
        packs_for=functools.partial(workspace_packs, opened, directory_packs),
        cache_size=settings.sessions_cache,
    )
    service = GatewayService(settings, opened, sessions, clock=clock, now=now, directory_packs=directory_packs)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        try:
            yield
        finally:
            await close_executor()
            if owns_store:
                opened.engine.dispose()

    app = FastAPI(title="Benchpress gateway", version=__version__, lifespan=lifespan)
    app.state.service = service
    app.state.store = opened
    app.state.limiter = RateLimiter(settings.requests_per_minute)
    app.add_middleware(BodySizeLimit, max_bytes=settings.max_body_bytes)
    app.add_exception_handler(RequestRejected, _rejected)
    app.include_router(router)
    return app
