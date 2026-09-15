"""`create_app`: the Benchpress gateway as a FastAPI application.

Each route (in `benchpress.gateway.routes`) authenticates through a `benchpress.gateway.deps` dependency, calls
`GatewayService` (or a `ReceiptSource`) and returns its model; the behaviour lives in the service. The body cap
is a pure ASGI middleware, so it holds whatever a route reads and however it reads it.
"""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, cast

import anyio
import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from benchpress import __version__
from benchpress.context import utc_now
from benchpress.gateway.approvals import ApprovalQueue
from benchpress.gateway.auth import RateLimiter
from benchpress.gateway.config import Settings
from benchpress.gateway.console import CONSOLE_DIST, mount_console
from benchpress.gateway.deps import authenticate, current_workspace
from benchpress.gateway.executors import executor_from_settings
from benchpress.gateway.metrics import GatewayMetrics, create_metrics
from benchpress.gateway.receipt_sources import DiskReceiptSource, ReceiptSource, StoreReceiptSource
from benchpress.gateway.routes import router
from benchpress.gateway.schemas import ReceiptDetail, ReceiptFilters, ReceiptPage
from benchpress.gateway.service import GatewayService, RequestRejected, load_directory_packs, workspace_packs
from benchpress.gateway.sessions import SessionRegistry
from benchpress.gateway.store import SqlIdempotencyStore, Store
from benchpress.tools import ToolExecutor
from benchpress.write_receipts import Clock

__all__ = ["BodySizeLimit", "HTTPMetrics", "create_app", "create_ui_app", "receipts_router"]

_logger = logging.getLogger("benchpress.gateway")


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


# ---- HTTP metrics ------------------------------------------------------------------------------


class HTTPMetrics:
    """Counts and times every HTTP request. The outermost middleware, so a 413 from `BodySizeLimit` is
    still counted (with route `"unmatched"`, since routing never ran).

    Reads `scope["route"].path` only after the inner app has handled the request, so an id embedded in a
    concrete URL (like a receipt id) never becomes a label value — only the route's template does.
    """

    def __init__(self, app: ASGIApp, metrics: GatewayMetrics) -> None:
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status_code = 500

        async def capturing_send(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, capturing_send)
        finally:
            route = scope.get("route")
            route_path = route.path if route is not None else "unmatched"
            self.metrics.http_requests.labels(route=route_path, method=scope["method"], code=str(status_code)).inc()
            self.metrics.http_seconds.labels(route=route_path).observe(time.perf_counter() - started)


# ---- exception mapping ------------------------------------------------------------------------


async def _rejected(_request: Request, exc: Exception) -> Response:
    rejected = cast(RequestRejected, exc)
    return JSONResponse(status_code=rejected.status, content={"detail": str(rejected), **rejected.extra})


# ---- /metrics ------------------------------------------------------------------------------------


async def _metrics_endpoint(request: Request) -> Response:
    """Requires a valid key (the same lookup and rate limit `current_workspace` applies, Ruling R16) unless
    `metrics_auth == "none"` — independent of `settings.auth`, so the two can differ."""
    service = cast(GatewayService, request.app.state.service)
    if service.settings.metrics_auth != "none":
        await authenticate(request, service)
    body, content_type = cast(GatewayMetrics, request.app.state.metrics).render()
    return Response(content=body, media_type=content_type)


# ---- receipts router (shared by `create_app` and `create_ui_app`) ------------------------------------


def receipts_router(source_for: Callable[[Request], Awaitable[ReceiptSource]]) -> APIRouter:
    """The three `/v1/receipts` routes, parameterized over where a `ReceiptSource` comes from.

    `create_app` passes a `source_for` that authenticates and returns a `StoreReceiptSource`;
    `create_ui_app` passes one that always returns its single `DiskReceiptSource`. Ruling R2: every route
    here names its path parameter `receipt_id`, in both apps.

    `source_for` is captured as a plain `Depends(source_for)` default (not `Annotated[..., Depends(...)]`):
    this module runs under `from __future__ import annotations`, which turns every annotation into a
    string FastAPI resolves later against the module's globals — and `source_for` is this factory's own
    parameter, never a module global, so an `Annotated` alias referencing it would fail to resolve. A
    default value is a normal expression evaluated right here, so the closure over `source_for` works.
    """
    router = APIRouter()

    @router.get("/v1/receipts")
    async def list_receipts(
        filters: Annotated[ReceiptFilters, Query()], source: ReceiptSource = Depends(source_for)  # noqa: B008
    ) -> ReceiptPage:
        return await source.list(filters)

    @router.get("/v1/receipts/{receipt_id}")
    async def get_receipt(
        receipt_id: str, source: ReceiptSource = Depends(source_for)  # noqa: B008
    ) -> ReceiptDetail:
        detail = await source.get(receipt_id)
        if detail is None:
            raise HTTPException(404, "no such receipt in this workspace")
        return detail

    @router.get("/v1/receipts/{receipt_id}/html", response_class=HTMLResponse)
    async def get_receipt_html(
        receipt_id: str, source: ReceiptSource = Depends(source_for)  # noqa: B008
    ) -> HTMLResponse:
        page = await source.html(receipt_id)
        if page is None:
            raise HTTPException(404, "no HTML page for this receipt")
        return HTMLResponse(page)

    return router


async def _store_receipt_source(request: Request) -> ReceiptSource:
    """`receipts_router`'s source for `create_app`: authenticate, then read that workspace's store rows."""
    workspace = await current_workspace(request)
    service = cast(GatewayService, request.app.state.service)
    return StoreReceiptSource(service.store, workspace)


# ---- the read-only local UI app ------------------------------------------------------------------


def create_ui_app(root: Path, *, dist: Path = CONSOLE_DIST) -> FastAPI:
    """A read-only local console over the plain receipt files under `root`: no auth, no store, no metrics,
    no body cap — it only ever serves `GET` requests. Meant for `benchpress ui`, browsing whatever a demo
    run, a shim's guard log, or a `VerifiedWrite` JSONL sink already wrote to disk.
    """
    source = DiskReceiptSource(root)

    async def _disk_receipt_source(_request: Request) -> ReceiptSource:
        return source

    app = FastAPI(title="Benchpress UI", version=__version__)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/v1/meta")
    async def meta() -> dict[str, str]:
        return {"mode": "local", "version": __version__, "root": str(root)}

    app.include_router(receipts_router(_disk_receipt_source))
    # `mount_console` is last: see the comment on its call in `create_app`.
    mount_console(app, dist)
    return app


# ---- the app ------------------------------------------------------------------------------------


async def _nothing_to_close() -> None:
    return None


async def _expire_sweep(approvals: ApprovalQueue, *, interval: float = 30.0) -> None:
    """Close every approval past its TTL, roughly every `interval` seconds, until cancelled.

    A single failing sweep (a transient DB error, a receipt-append failure inside `_close`) must not end the
    sweep for the life of the process: it is logged at WARNING and the loop keeps going. Only cancellation
    (shutdown) ends it — `Exception` never includes the cancellation exception, so that still propagates.
    """
    while True:
        await anyio.sleep(interval)
        try:
            await approvals.expire_due()
        except Exception:
            _logger.warning("approval expiry sweep failed; will retry on the next interval")


def create_app(
    settings: Settings,
    *,
    store: Store | None = None,
    executor: ToolExecutor | None = None,
    clock: Clock = utc_now,
    now: Callable[[], float] = time.time,
    http: httpx.AsyncClient | None = None,
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
    owns_http = http is None
    http_client = http if http is not None else httpx.AsyncClient()

    claims = SqlIdempotencyStore(opened, lease_seconds=settings.idempotency_lease_seconds, now=now)
    sessions = SessionRegistry(
        opened,
        executor,
        claims=claims,
        packs_for=functools.partial(workspace_packs, opened, directory_packs),
        cache_size=settings.sessions_cache,
    )
    metrics = create_metrics(extra_providers=[upstream.provider for upstream in settings.upstreams])
    approvals = ApprovalQueue(
        opened,
        ttl_seconds=settings.approval_ttl_seconds,
        webhook_url=settings.approval_webhook_url,
        webhook_secret=settings.approval_webhook_secret,
        now=now,
        clock=clock,
        http=http_client,
        metrics=metrics,
    )
    service = GatewayService(
        settings,
        opened,
        sessions,
        clock=clock,
        now=now,
        approvals=approvals,
        metrics=metrics,
        directory_packs=directory_packs,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        # The sweeper's own cancel scope covers this host task too, so cancelling it here (to stop the sweep
        # before shutdown) must happen *inside* the `async with`, and every close that needs its own
        # checkpoints to complete (a real upstream client's `aclose`, the http client's, the store engine's
        # dispose) must happen *after* the task group has exited — otherwise the still-cancelled scope
        # cancels those closes too, and they get silently skipped rather than run.
        try:
            async with anyio.create_task_group() as sweeper:
                if settings.approval_rules:
                    sweeper.start_soon(_expire_sweep, approvals)
                try:
                    yield
                finally:
                    sweeper.cancel_scope.cancel()
        finally:
            await close_executor()
            if owns_http:
                await http_client.aclose()
            if owns_store:
                opened.engine.dispose()

    app = FastAPI(title="Benchpress gateway", version=__version__, lifespan=lifespan)
    app.state.service = service
    app.state.store = opened
    app.state.metrics = metrics
    app.state.limiter = RateLimiter(settings.requests_per_minute)
    # `HTTPMetrics` is added last, so it wraps `BodySizeLimit` and stays the outermost middleware: a request
    # `BodySizeLimit` rejects with 413 is still counted, under route "unmatched".
    app.add_middleware(BodySizeLimit, max_bytes=settings.max_body_bytes)
    app.add_middleware(HTTPMetrics, metrics=metrics)
    app.add_exception_handler(RequestRejected, _rejected)
    app.include_router(router)
    app.include_router(receipts_router(_store_receipt_source))
    app.add_api_route("/metrics", _metrics_endpoint, methods=["GET"])
    if settings.console:
        # `mount_console` must be the LAST registration: a static mount at "/" answers every request that
        # no earlier route claimed, and Starlette tries routes in registration order, so it would shadow
        # any route registered after it. Task 11 registers `/mcp` before this line for the same reason.
        mount_console(app)
    return app
