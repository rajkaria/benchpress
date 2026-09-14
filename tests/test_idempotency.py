"""Lease-based write claims: the store every VerifiedWrite instance can share."""

from __future__ import annotations

from benchpress.idempotency import InMemoryIdempotencyStore


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def test_first_claim_wins_and_a_twin_is_in_flight() -> None:
    store = InMemoryIdempotencyStore()
    assert await store.claim("s", "fp1", holder="a") == "claimed"
    assert await store.claim("s", "fp1", holder="b") == "in_flight"
    assert await store.claim("other-scope", "fp1", holder="c") == "claimed"


async def test_a_success_is_done_forever() -> None:
    store = InMemoryIdempotencyStore()
    await store.claim("s", "fp1", holder="a")
    await store.complete("s", "fp1", holder="a", succeeded=True)
    assert await store.claim("s", "fp1", holder="b") == "done"


async def test_a_failure_releases_the_claim_for_a_retry() -> None:
    store = InMemoryIdempotencyStore()
    await store.claim("s", "fp1", holder="a")
    await store.complete("s", "fp1", holder="a", succeeded=False)
    assert await store.claim("s", "fp1", holder="b") == "claimed"


async def test_an_expired_lease_is_taken_over() -> None:
    clock = FakeClock()
    store = InMemoryIdempotencyStore(lease_seconds=30.0, clock=clock)
    await store.claim("s", "fp1", holder="a")
    clock.now += 29.0
    assert await store.claim("s", "fp1", holder="b") == "in_flight"
    clock.now += 2.0
    assert await store.claim("s", "fp1", holder="c") == "claimed"


async def test_a_stale_holders_release_does_not_clobber_a_newer_holders_claim() -> None:
    """Ruling R4: after a lease takeover, the original holder's own `complete(succeeded=False)` must not
    release the new holder's claim — otherwise a slow-to-clean-up crashed holder could free a key a live
    holder is still working on."""
    clock = FakeClock()
    store = InMemoryIdempotencyStore(lease_seconds=30.0, clock=clock)
    assert await store.claim("s", "fp1", holder="A") == "claimed"
    clock.now += 31.0  # A's lease has expired
    assert await store.claim("s", "fp1", holder="B") == "claimed"  # B takes over

    await store.complete("s", "fp1", holder="A", succeeded=False)  # stale release: must be a no-op
    assert await store.claim("s", "fp1", holder="C") == "in_flight"  # B's claim still stands

    await store.complete("s", "fp1", holder="B", succeeded=False)  # B's own release: works
    assert await store.claim("s", "fp1", holder="D") == "claimed"


async def test_a_stale_holders_success_still_marks_the_key_done() -> None:
    """A stale holder's `complete(succeeded=True)` is unconditional: the write actually happened, so the key
    must go `done` and any in-flight entry must clear, regardless of who holds the lease now."""
    clock = FakeClock()
    store = InMemoryIdempotencyStore(lease_seconds=30.0, clock=clock)
    assert await store.claim("s", "fp1", holder="A") == "claimed"
    clock.now += 31.0
    assert await store.claim("s", "fp1", holder="B") == "claimed"  # B takes over

    await store.complete("s", "fp1", holder="A", succeeded=True)  # stale success: still counts
    assert await store.claim("s", "fp1", holder="C") == "done"
