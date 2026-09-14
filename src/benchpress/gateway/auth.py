"""Gateway auth building blocks: bearer-token parsing, a per-key rate limiter, and bootstrap workspace setup.

Unlike `benchpress.gateway.config`, this module may import the store — `ensure_bootstrap` needs it to
create the first workspace and key a fresh gateway starts with.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from benchpress.gateway.config import ConfigError
from benchpress.gateway.store import Store

__all__ = ["RateLimiter", "bearer_token", "ensure_bootstrap"]

_BEARER_PREFIX = "bearer "
_MIN_BOOTSTRAP_KEY_LENGTH = 32


def _empty_buckets() -> dict[str, tuple[float, float]]:
    return {}


def bearer_token(header: str | None) -> str | None:
    """Extract the token from an `Authorization: Bearer <token>` header, case-insensitively."""
    if header is None or header[: len(_BEARER_PREFIX)].lower() != _BEARER_PREFIX:
        return None
    return header[len(_BEARER_PREFIX) :].strip() or None


@dataclass
class RateLimiter:
    """A per-key token bucket: capacity `per_minute`, refilling at `per_minute / 60` tokens/second."""

    per_minute: int
    clock: Callable[[], float] = time.monotonic
    _buckets: dict[str, tuple[float, float]] = field(default_factory=_empty_buckets, init=False, repr=False)

    def allow(self, key: str) -> bool:
        now = self.clock()
        capacity = float(self.per_minute)
        refill_per_second = capacity / 60.0
        tokens, last = self._buckets.get(key, (capacity, now))
        tokens = min(capacity, tokens + (now - last) * refill_per_second)
        if tokens < 1.0:
            self._buckets[key] = (tokens, now)
            return False
        self._buckets[key] = (tokens - 1.0, now)
        return True


def ensure_bootstrap(store: Store, env: Mapping[str, str]) -> str | None:
    """Create a `default` workspace with a `bootstrap` key if the store has no workspace yet.

    The key's plaintext comes from `BENCHPRESS_BOOTSTRAP_KEY` when set (validated before anything is
    written, so a bad value never leaves a half-created workspace behind); otherwise one is generated.
    Returns the plaintext only when it was generated, so the caller can print it exactly once.
    """
    if store.workspaces():
        return None
    bootstrap_key = env.get("BENCHPRESS_BOOTSTRAP_KEY")
    if bootstrap_key is not None and (
        not bootstrap_key.startswith("bp_") or len(bootstrap_key) < _MIN_BOOTSTRAP_KEY_LENGTH
    ):
        raise ConfigError(
            f"BENCHPRESS_BOOTSTRAP_KEY must start with 'bp_' and be at least {_MIN_BOOTSTRAP_KEY_LENGTH} characters"
        )
    workspace = store.create_workspace("default")
    if bootstrap_key is not None:
        store.create_api_key(workspace.id, "bootstrap", plaintext=bootstrap_key)
        return None
    _row, plaintext = store.create_api_key(workspace.id, "bootstrap")
    return plaintext
