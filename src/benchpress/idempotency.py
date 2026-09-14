"""Write claims that de-duplicate identical writes across `VerifiedWrite` instances (and gateway replicas).

A claim is keyed by `(scope, fingerprint)`. The first caller claims it; a twin arriving while it is in flight is told
`in_flight`; once the write succeeds the key is `done` forever. A failed write releases its claim so a retry is sent.
An in-flight claim older than the lease is taken over, so a crashed holder never blocks a key permanently.

Every claim carries a `holder` token (Ruling R4). `complete(succeeded=False)` — the "release" path — only clears the
in-flight entry when its holder still matches: once a lease has been taken over, the original (stale) holder's own
release can no longer clobber the new holder's claim. `complete(succeeded=True)` is unconditional: the write really
happened, whoever holds the lease now, so the key must go `done` and any in-flight entry must clear regardless of
holder.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol

ClaimResult = Literal["claimed", "done", "in_flight"]


class IdempotencyStore(Protocol):
    async def claim(self, scope: str, fingerprint: str, *, holder: str) -> ClaimResult: ...

    async def complete(self, scope: str, fingerprint: str, *, holder: str, succeeded: bool) -> None: ...


@dataclass
class InMemoryIdempotencyStore:
    """One process's claims. Share one instance between `VerifiedWrite`s to de-duplicate across them."""

    lease_seconds: float = 300.0
    clock: Callable[[], float] = time.monotonic
    _done: set[tuple[str, str]] = field(default_factory=set[tuple[str, str]])
    _in_flight: dict[tuple[str, str], tuple[str, float]] = field(
        default_factory=dict[tuple[str, str], tuple[str, float]]
    )

    async def claim(self, scope: str, fingerprint: str, *, holder: str) -> ClaimResult:
        # No await between the checks and the write: atomic on one event loop.
        key = (scope, fingerprint)
        if key in self._done:
            return "done"
        entry = self._in_flight.get(key)
        now = self.clock()
        if entry is not None and now - entry[1] < self.lease_seconds:
            return "in_flight"
        self._in_flight[key] = (holder, now)
        return "claimed"

    async def complete(self, scope: str, fingerprint: str, *, holder: str, succeeded: bool) -> None:
        key = (scope, fingerprint)
        if succeeded:
            # The write happened; go done unconditionally, whoever holds the lease now.
            self._in_flight.pop(key, None)
            self._done.add(key)
            return
        # A release only clears the entry this holder itself owns — a stale holder must never
        # clobber a newer holder's claim after a lease takeover.
        entry = self._in_flight.get(key)
        if entry is not None and entry[0] == holder:
            del self._in_flight[key]
