"""Host twins in-process: one uvicorn server per (twin × {data plane, admin plane}) on loopback.

Design (docs/TRACK-DEVSIM.md §2): one `PortBlock` per trial so parallel trials never collide,
`uvicorn.Server` driven directly (startup → main_loop task → shutdown) so no signal handlers are
installed, and a health probe before a twin is reported ready. Ports are pre-bound listening
sockets handed to uvicorn, which removes the allocate-then-bind race entirely.

Seeding contract: `store.seed()` receives the provider's *slice* of the scenario `seed_config`
(`seed_config["stripe"]` → `{"customers": [...], "products": [...]}`), matching the executable
example in tests/devsim/test_base.py.
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

import httpx
import uvicorn
from starlette.applications import Starlette

from devsim.twins import PROVIDER_ROLES, Store, TwinSpec, load_spec
from devsim.twins.stub import make_stub_data_app, make_stub_spec

LOOPBACK = "127.0.0.1"
START_TIMEOUT_SECONDS = 15.0
PROBE_INTERVAL_SECONDS = 0.05


class TwinStartupError(RuntimeError):
    """A twin server did not come up or did not serve a JSON admin state."""


class PortBlock:
    """Pre-bound listening sockets on loopback. Allocate one block per trial."""

    def __init__(self, sockets: Sequence[socket.socket]) -> None:
        self._sockets: list[socket.socket] = list(sockets)
        self._handed_out: list[socket.socket] = []

    @classmethod
    def allocate(cls, count: int) -> PortBlock:
        if count < 1:
            raise ValueError("a port block needs at least one port")
        sockets: list[socket.socket] = []
        try:
            for _ in range(count):
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((LOOPBACK, 0))
                sock.listen(128)
                sock.setblocking(False)
                sockets.append(sock)
        except OSError:
            for sock in sockets:
                sock.close()
            raise
        return cls(sockets)

    @property
    def ports(self) -> tuple[int, ...]:
        return tuple(port_of(sock) for sock in [*self._handed_out, *self._sockets])

    def take(self) -> socket.socket:
        """Hand the next unused socket to a server (the server then owns it)."""

        if not self._sockets:
            raise RuntimeError("port block exhausted")
        sock = self._sockets.pop(0)
        self._handed_out.append(sock)
        return sock

    def close(self) -> None:
        """Close sockets that were never handed to a server."""

        for sock in self._sockets:
            sock.close()
        self._sockets.clear()


def port_of(sock: socket.socket) -> int:
    return int(sock.getsockname()[1])


class TwinServer:
    """One uvicorn server for one ASGI app on one pre-bound loopback socket."""

    def __init__(self, app: Starlette, sock: socket.socket, *, label: str) -> None:
        self.label = label
        self._sock = sock
        self.port = port_of(sock)
        self.url = f"http://{LOOPBACK}:{self.port}"
        self._config = uvicorn.Config(
            app=app,
            host=LOOPBACK,
            port=self.port,
            log_level="warning",
            log_config=None,
            lifespan="off",
            access_log=False,
            timeout_graceful_shutdown=5,
        )
        self._server = uvicorn.Server(self._config)
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError(f"{self.label}: already started")
        config = self._config
        if not config.loaded:
            config.load()
        self._server.lifespan = config.lifespan_class(config)
        await self._server.startup(sockets=[self._sock])
        if not self._server.started:
            raise TwinStartupError(f"{self.label}: uvicorn did not start")
        self._task = asyncio.create_task(self._server.main_loop(), name=f"devsim-{self.label}")
        try:
            await wait_for_http(self.url + "/healthz", label=self.label)
        except TwinStartupError:
            await self.stop()
            raise

    async def stop(self) -> None:
        if self._task is None:
            return
        self._server.should_exit = True
        task = self._task
        self._task = None
        try:
            await asyncio.wait_for(task, timeout=10)
        except TimeoutError:
            task.cancel()
        if self._server.started:
            await self._server.shutdown(sockets=[self._sock])
        self._sock.close()


async def wait_for_http(
    url: str,
    *,
    label: str,
    timeout_seconds: float = START_TIMEOUT_SECONDS,
) -> int:
    """Poll until the URL answers any HTTP status; return that status."""

    deadline = asyncio.get_running_loop().time() + timeout_seconds
    last_error: Exception | None = None
    async with httpx.AsyncClient(timeout=2.0) as client:
        while True:
            try:
                response = await client.get(url)
                return response.status_code
            except httpx.TransportError as error:
                last_error = error
            if asyncio.get_running_loop().time() >= deadline:
                raise TwinStartupError(f"{label}: no HTTP answer from {url} ({type(last_error).__name__})")
            await asyncio.sleep(PROBE_INTERVAL_SECONDS)


async def verify_admin_state(admin_url: str, paths: Sequence[str], *, label: str) -> str:
    """Return the first admin path that serves a 2xx JSON object (the capturer's requirement)."""

    failures: list[str] = []
    async with httpx.AsyncClient(timeout=5.0) as client:
        for path in paths:
            response = await client.get(admin_url.rstrip("/") + path)
            if not 200 <= response.status_code < 300:
                failures.append(f"{path} → HTTP {response.status_code}")
                continue
            try:
                payload: object = response.json()
            except json.JSONDecodeError:
                failures.append(f"{path} → not JSON")
                continue
            if not isinstance(payload, dict):
                failures.append(f"{path} → JSON {type(payload).__name__}, not an object")
                continue
            return path
    raise TwinStartupError(f"{label}: admin state unavailable ({'; '.join(failures)})")


def spec_for(provider: str) -> TwinSpec:
    """The provider's twin module if it exists, otherwise a stub spec for that provider.

    Only a missing `devsim.twins.<provider>` module falls back to the stub; an ImportError raised
    *inside* a real twin module propagates, so a broken twin is never silently replaced.
    """

    try:
        return load_spec(provider)
    except ModuleNotFoundError as error:
        if error.name != f"devsim.twins.{provider}":
            raise
    return make_stub_spec(provider, PROVIDER_ROLES.get(provider, provider))


def is_stub_spec(spec: TwinSpec) -> bool:
    return spec.make_data_app is make_stub_data_app


@dataclass
class RunningTwin:
    provider: str
    role: str
    spec: TwinSpec
    store: Store
    data: TwinServer
    admin: TwinServer
    admin_state_path: str
    is_stub: bool

    @property
    def base_url(self) -> str:
        return self.data.url

    @property
    def admin_url(self) -> str:
        return self.admin.url

    async def stop(self) -> None:
        await asyncio.gather(self.data.stop(), self.admin.stop())


@dataclass
class RunningTwins:
    """Every twin of one run, plus the port block they were allocated from."""

    seed_key: str
    port_block: PortBlock
    twins: dict[str, RunningTwin] = field(default_factory=lambda: dict[str, RunningTwin]())

    def __getitem__(self, provider: str) -> RunningTwin:
        return self.twins[provider]

    @property
    def providers(self) -> tuple[str, ...]:
        return tuple(sorted(self.twins))

    @property
    def stores(self) -> dict[str, Store]:
        return {provider: twin.store for provider, twin in sorted(self.twins.items())}

    def urls(self) -> dict[str, dict[str, str]]:
        return {
            provider: {"base_url": twin.base_url, "admin_url": twin.admin_url}
            for provider, twin in sorted(self.twins.items())
        }

    def env_lines(self, *, export: bool = False) -> list[str]:
        prefix = "export " if export else ""
        lines: list[str] = []
        for provider, twin in sorted(self.twins.items()):
            key = provider.upper()
            lines.append(f"{prefix}DEVSIM_{key}_URL={twin.base_url}")
            lines.append(f"{prefix}DEVSIM_{key}_ADMIN_URL={twin.admin_url}")
        return lines

    async def stop(self) -> None:
        await asyncio.gather(*(twin.stop() for twin in self.twins.values()))
        self.port_block.close()


async def serve_twins(
    providers: Sequence[str],
    seed_config: Mapping[str, Any],
    seed_key: str,
) -> RunningTwins:
    """Start data + admin servers for every provider, seeded deterministically from `seed_key`."""

    ordered = list(dict.fromkeys(providers))
    if not ordered:
        raise ValueError("at least one provider is required")
    block = PortBlock.allocate(2 * len(ordered))
    running = RunningTwins(seed_key=seed_key, port_block=block)
    try:
        for provider in ordered:
            spec = spec_for(provider)
            store = spec.make_store(seed_key)
            raw_slice = seed_config.get(provider, {})
            provider_slice: Mapping[str, Any] = (
                cast(Mapping[str, Any], raw_slice) if isinstance(raw_slice, Mapping) else {}
            )
            store.seed(provider_slice)
            data = TwinServer(spec.make_data_app(store), block.take(), label=f"{provider}-data")
            admin = TwinServer(spec.make_admin_app(store), block.take(), label=f"{provider}-admin")
            await data.start()
            try:
                await admin.start()
            except TwinStartupError:
                await data.stop()
                raise
            twin = RunningTwin(
                provider=provider,
                role=spec.role,
                spec=spec,
                store=store,
                data=data,
                admin=admin,
                admin_state_path="",
                is_stub=is_stub_spec(spec),
            )
            running.twins[provider] = twin
            twin.admin_state_path = await verify_admin_state(admin.url, spec.admin_paths, label=f"{provider}-admin")
    except BaseException:
        await running.stop()
        raise
    return running


__all__ = [
    "LOOPBACK",
    "PortBlock",
    "RunningTwin",
    "RunningTwins",
    "TwinServer",
    "TwinStartupError",
    "is_stub_spec",
    "port_of",
    "serve_twins",
    "spec_for",
    "verify_admin_state",
    "wait_for_http",
]
