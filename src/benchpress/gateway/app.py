"""`create_app`: the Benchpress gateway as a FastAPI application.

Each route (in `benchpress.gateway.routes`) authenticates through a `benchpress.gateway.deps` dependency, calls
`GatewayService` (or a `ReceiptSource`) and returns its model; the behaviour lives in the service. The body cap
is a pure ASGI middleware, so it holds whatever a route reads and however it reads it.
"""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import cast

import anyio
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from benchpress import __version__
from benchpress.context import utc_now
from benchpress.gateway.approvals import ApprovalQueue
from benchpress.gateway.auth import RateLimiter
from benchpress.gateway.config import Settings
from benchpress.gateway.executors import executor_from_settings
from benchpress.gateway.routes import router
from benchpress.gateway.service import GatewayService, RequestRejected, load_directory_packs, workspace_packs
from benchpress.gateway.sessions import SessionRegistry
from benchpress.gateway.store import SqlIdempotencyStore, Store
from benchpress.tools import ToolExecutor
from benchpress.write_receipts import Clock

__all__ = ["BodySizeLimit", "create_app"]

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


# ---- exception mapping ------------------------------------------------------------------------


async def _rejected(_request: Request, exc: Exception) -> Response:
    rejected = cast(RequestRejected, exc)
    return JSONResponse(status_code=rejected.status, content={"detail": str(rejected), **rejected.extra})


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
    approvals = ApprovalQueue(
        opened,
        ttl_seconds=settings.approval_ttl_seconds,
        webhook_url=settings.approval_webhook_url,
        webhook_secret=settings.approval_webhook_secret,
        now=now,
        clock=clock,
        http=http_client,
    )
    service = GatewayService(
        settings, opened, sessions, clock=clock, now=now, approvals=approvals, directory_packs=directory_packs
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
    app.state.limiter = RateLimiter(settings.requests_per_minute)
    app.add_middleware(BodySizeLimit, max_bytes=settings.max_body_bytes)
    app.add_exception_handler(RequestRejected, _rejected)
    app.include_router(router)
    return app
