"""Write claims that de-duplicate identical writes across `VerifiedWrite` instances (and gateway replicas).

A claim is keyed by `(scope, fingerprint)`. The first caller claims it; a twin arriving while it is in flight is told
`in_flight`; once the write succeeds the key is `done` forever. A failed write releases its claim so a retry is sent.
An in-flight claim older than the lease is taken over, so a crashed holder never blocks a key permanently.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol

ClaimResult = Literal["claimed", "done", "in_flight"]


class IdempotencyStore(Protocol):
    async def claim(self, scope: str, fingerprint: str) -> ClaimResult: ...

    async def complete(self, scope: str, fingerprint: str, *, succeeded: bool) -> None: ...


@dataclass
class InMemoryIdempotencyStore:
    """One process's claims. Share one instance between `VerifiedWrite`s to de-duplicate across them."""

    lease_seconds: float = 300.0
    clock: Callable[[], float] = time.monotonic
    _done: set[tuple[str, str]] = field(default_factory=set[tuple[str, str]])
    _in_flight: dict[tuple[str, str], float] = field(default_factory=dict[tuple[str, str], float])

    async def claim(self, scope: str, fingerprint: str) -> ClaimResult:
        # No await between the checks and the write: atomic on one event loop.
        key = (scope, fingerprint)
        if key in self._done:
            return "done"
        started = self._in_flight.get(key)
        now = self.clock()
        if started is not None and now - started < self.lease_seconds:
            return "in_flight"
        self._in_flight[key] = now
        return "claimed"

    async def complete(self, scope: str, fingerprint: str, *, succeeded: bool) -> None:
        key = (scope, fingerprint)
        self._in_flight.pop(key, None)
        if succeeded:
            self._done.add(key)
