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
    assert await store.claim("s", "fp1") == "claimed"
    assert await store.claim("s", "fp1") == "in_flight"
    assert await store.claim("other-scope", "fp1") == "claimed"


async def test_a_success_is_done_forever() -> None:
    store = InMemoryIdempotencyStore()
    await store.claim("s", "fp1")
    await store.complete("s", "fp1", succeeded=True)
    assert await store.claim("s", "fp1") == "done"


async def test_a_failure_releases_the_claim_for_a_retry() -> None:
    store = InMemoryIdempotencyStore()
    await store.claim("s", "fp1")
    await store.complete("s", "fp1", succeeded=False)
    assert await store.claim("s", "fp1") == "claimed"


async def test_an_expired_lease_is_taken_over() -> None:
    clock = FakeClock()
    store = InMemoryIdempotencyStore(lease_seconds=30.0, clock=clock)
    await store.claim("s", "fp1")
    clock.now += 29.0
    assert await store.claim("s", "fp1") == "in_flight"
    clock.now += 2.0
    assert await store.claim("s", "fp1") == "claimed"
