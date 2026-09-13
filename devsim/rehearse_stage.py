"""A devsim-backed `benchpress.rehearse.Stage`: fresh twins per run, fully in-process.

Every stage builds new twin stores from the caller's seed and serves their data planes through
`httpx.ASGITransport` behind the real `RealAppGateway` (same validation, headers, trace shape as
a production run). No socket is ever bound: each provider gets a distinct loopback host name
(`127.0.0.<n>`) that only the in-process router understands.

Not shipped in the `benchpress-agent` wheel.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

import httpx

from benchpress.realapp import RealAppGateway, config_from_env
from benchpress.rehearse import JsonState, Stage, StageFactory
from devsim.server import spec_for
from devsim.twins.base import Store

StoreMutation = Callable[[Mapping[str, Store]], None]


class HostRouter(httpx.AsyncBaseTransport):
    """Dispatch a request to the in-process ASGI app registered for its host."""

    def __init__(self, transports: Mapping[str, httpx.ASGITransport]) -> None:
        self._transports = dict(transports)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        transport = self._transports.get(request.url.host)
        if transport is None:
            return httpx.Response(502, json={"error": f"rehearsal stage: no twin for host {request.url.host!r}"})
        return await transport.handle_async_request(request)

    async def aclose(self) -> None:
        for transport in self._transports.values():
            await transport.aclose()


class TwinStage:
    """One isolated copy of the seeded workspace."""

    def __init__(
        self,
        providers: Sequence[str],
        seed_config: Mapping[str, Any],
        *,
        seed_key: str = "rehearsal",
        mutate: StoreMutation | None = None,
    ) -> None:
        ordered = list(dict.fromkeys(providers))
        if not ordered:
            raise ValueError("a stage needs at least one provider")
        self.providers = tuple(ordered)
        self.stores: dict[str, Store] = {}
        transports: dict[str, httpx.ASGITransport] = {}
        env: dict[str, str] = {}
        for number, provider in enumerate(ordered, start=10):
            spec = spec_for(provider)
            store = spec.make_store(seed_key)
            raw_slice = seed_config.get(provider, {})
            store.seed(cast(Mapping[str, Any], raw_slice) if isinstance(raw_slice, Mapping) else {})
            self.stores[provider] = store
            host = f"127.0.0.{number}"
            transports[host] = httpx.ASGITransport(app=spec.make_data_app(store))
            env[f"DEVSIM_{provider.upper()}_URL"] = f"http://{host}"
        if mutate is not None:
            mutate(self.stores)
        # `env` is explicit, so nothing from the process environment (or `.env`) is consulted.
        configs = [config_from_env(provider, env) for provider in ordered]
        self.gateway = RealAppGateway(configs, env=env, transport=HostRouter(transports))

    async def execute_tool(self, tool_name: str, tool_input: dict[str, Any]) -> object:
        return await self.gateway.execute_tool(tool_name, tool_input)

    async def snapshot(self) -> JsonState:
        return {provider: store.admin_state() for provider, store in sorted(self.stores.items())}

    async def aclose(self) -> None:
        await self.gateway.aclose()


def twin_stage_factory(
    providers: Sequence[str],
    seed_config: Mapping[str, Any],
    *,
    seed_key: str = "rehearsal",
    mutate: StoreMutation | None = None,
) -> StageFactory:
    """Every call returns a brand-new `TwinStage` seeded identically."""

    async def factory() -> Stage:
        return TwinStage(providers, seed_config, seed_key=seed_key, mutate=mutate)

    return factory


def stage_from_args(providers: Sequence[str], seed_file: str | None) -> StageFactory:
    """`benchpress rehearse --stage devsim.rehearse_stage:stage_from_args --seed seed.json`."""
    seed: Mapping[str, Any] = {}
    if seed_file:
        loaded: object = json.loads(Path(seed_file).read_text())
        if not isinstance(loaded, Mapping):
            raise ValueError("the seed file must hold a JSON object keyed by provider")
        seed = cast(Mapping[str, Any], loaded)
    return twin_stage_factory(providers, seed)


__all__ = ["HostRouter", "TwinStage", "stage_from_args", "twin_stage_factory"]
