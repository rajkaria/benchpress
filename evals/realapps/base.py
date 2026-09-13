"""Shared contract for real-app seeders.

Every app driver implements `RealApp`: load one provider's slice of a scenario `seed_config`
into a scratch account, snapshot the state we grade on, and reset to a clean baseline. These
are harness operations — the agent under test never sees them, and the agent's gateway never
performs deletes.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx


class ScratchGuardError(RuntimeError):
    """Raised when a driver would touch anything that is not a scratch/test account."""


def require_scratch_ok() -> None:
    if os.environ.get("BENCHPRESS_SCRATCH_OK") != "1":
        raise ScratchGuardError("set BENCHPRESS_SCRATCH_OK=1 to allow seeding/resetting scratch accounts")


@dataclass
class SeedManifest:
    """Everything a seed created, so reset can remove exactly that (plus anything newer)."""

    scenario_id: str
    seeded_at: float = field(default_factory=time.time)
    created: dict[str, dict[str, list[str]]] = field(default_factory=lambda: dict[str, dict[str, list[str]]]())
    aliases: dict[str, dict[str, str]] = field(default_factory=lambda: dict[str, dict[str, str]]())

    def add(self, app: str, collection: str, resource_id: str) -> None:
        self.created.setdefault(app, {}).setdefault(collection, []).append(resource_id)

    def alias(self, app: str, label: str, resource_id: str) -> None:
        self.aliases.setdefault(app, {})[label] = resource_id

    def ids(self, app: str, collection: str) -> list[str]:
        return list(self.created.get(app, {}).get(collection, []))

    def counts(self) -> dict[str, dict[str, int]]:
        return {app: {name: len(ids) for name, ids in cols.items()} for app, cols in self.created.items()}

    def dump(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.__dict__, indent=2, sort_keys=True) + "\n")

    @classmethod
    def load(cls, path: Path) -> SeedManifest:
        raw = json.loads(path.read_text())
        manifest = cls(scenario_id=str(raw["scenario_id"]), seeded_at=float(raw["seeded_at"]))
        manifest.created = dict(raw.get("created", {}))
        manifest.aliases = dict(raw.get("aliases", {}))
        return manifest


@dataclass(frozen=True)
class SeedResult:
    app: str
    counts: Mapping[str, int]
    notes: tuple[str, ...] = ()


class RealAppClient:
    """Thin authenticated httpx wrapper with bounded retries on 429/5xx."""

    def __init__(self, base_url: str, headers: Mapping[str, str], *, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._headers = dict(headers)
        self._client = httpx.AsyncClient(base_url=self.base_url, headers=self._headers, timeout=timeout)

    def set_headers(self, headers: Mapping[str, str]) -> None:
        self._headers = dict(headers)
        self._client.headers.update(self._headers)

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | Sequence[tuple[str, str]] | None = None,
        json_body: object | None = None,
        data: Mapping[str, Any] | None = None,
        retries: int = 3,
    ) -> httpx.Response:
        attempt = 0
        while True:
            query: Mapping[str, str] | tuple[tuple[str, str], ...] | None = None
            if isinstance(params, Mapping):
                query = dict(params)
            elif params is not None:
                query = tuple(params)
            response = await self._client.request(method, path, params=query, json=json_body, data=data)
            if response.status_code in {429, 502, 503, 504} and attempt < retries:
                attempt += 1
                retry_after = response.headers.get("retry-after", "")
                numeric = retry_after.replace(".", "", 1).isdigit()
                delay = float(retry_after) if numeric else 2.0 * attempt
                await _sleep(min(delay, 30.0))
                continue
            return response

    async def aclose(self) -> None:
        await self._client.aclose()


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)


class RealApp(Protocol):
    """One provider's seed/snapshot/reset driver."""

    app: str

    async def seed(self, seed_config: Mapping[str, Any], manifest: SeedManifest) -> SeedResult: ...

    async def snapshot(self) -> dict[str, Any]: ...

    async def reset(self, manifest: SeedManifest) -> None: ...

    async def verify_clean(self) -> list[str]:
        """Return a list of residue descriptions; empty means clean."""
        ...

    async def aclose(self) -> None: ...
