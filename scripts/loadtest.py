"""Measure write latency: in process through `VerifiedWrite`, or against a running gateway at a fixed request rate.

    uv run python scripts/loadtest.py --in-process 5000
    uv run python scripts/loadtest.py --url http://127.0.0.1:8798 --key "$KEY" --rps 500 --seconds 20

Prints one JSON object (milliseconds rounded to 3 places).

* `--in-process N` runs N distinct HubSpot-shaped writes, each with a read-back, through one
  `VerifiedWrite(make_echo_executor(), ...)` and times every `run` with `time.perf_counter()`, so the in-memory
  executor's own (near-zero) cost is included. Every write must come back `verified`, or the run stops: a load test
  that silently measured refusals would be measuring the wrong thing.
* `--url URL --key KEY` creates one gateway session, then drives an open loop: request `i` is due at
  `start + i / rps` whether or not earlier requests have answered, with at most `--concurrency` in flight. Every request
  is a distinct write. `p50_ms`/`p99_ms`/`max_ms` run from each request's due time to its response, so time spent
  waiting for a free concurrency slot counts (no coordinated omission); `service_p50_ms`/`service_p99_ms` run from
  the moment the request was actually sent. Every non-2xx response, including 429 from the gateway's rate limit,
  counts in `http_errors`, and every transport error (a refused or reset connection, or no response within the
  client's 30 s timeout) in `transport_errors`; `errors` is their sum. `achieved_rps` is 2xx responses per second of
  wall time, from the first due time to the last response.

Point `--url` only at a gateway serving a harmless executor, such as
`benchpress serve --executor benchpress.gateway.testing:make_echo_executor`: every request is a real write.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import time
from collections.abc import Mapping, Sequence

import httpx

from benchpress.context import Action, Context, ReadBack
from benchpress.gateway.testing import make_echo_executor
from benchpress.verified import VerifiedWrite

PROMPT = "Load test writes to loadtest.example"


def percentile(values: Sequence[float], q: float) -> float:
    """The nearest-rank `q`th percentile (0 < q <= 100): the smallest value at least `q`% of `values` do not exceed."""
    if not values:
        raise ValueError("percentile of no values")
    if not 0 < q <= 100:
        raise ValueError(f"percentile must be in (0, 100], got {q!r}")
    ordered = sorted(values)
    rank = math.ceil(q / 100 * len(ordered))
    return ordered[min(max(rank, 1), len(ordered)) - 1]


def load_write(i: int) -> Action:
    """Write `i`: a HubSpot company description update with a read-back, on its own record."""
    record = str(9000 + i)
    path = f"/crm/v3/objects/companies/{record}"
    return Action(
        id=f"load-{i}",
        kind="update",
        provider="hubspot",
        method="PATCH",
        path=path,
        body={"properties": {"description": f"Load test write {i}"}},
        fields=("description",),
        target_refs=(f"company:{record}",),
        readback=ReadBack(path=path, query={"properties": "description"}, field_path="properties.description"),
    )


def _summary(latencies_ms: Sequence[float]) -> dict[str, float]:
    return {
        "p50_ms": percentile(latencies_ms, 50),
        "p99_ms": percentile(latencies_ms, 99),
        "max_ms": max(latencies_ms),
        "mean_ms": statistics.fmean(latencies_ms),
    }


async def in_process(n: int) -> dict[str, float]:
    """Latency of `n` sequential verified writes through one `VerifiedWrite` over the in-memory echo executor."""
    if n < 1:
        raise ValueError("in_process needs at least one write")
    writer = VerifiedWrite(make_echo_executor(), context=Context(user_prompt=PROMPT))
    actions = [load_write(i) for i in range(n)]
    latencies_ms: list[float] = []
    for action in actions:
        started = time.perf_counter()
        outcome = await writer.run(action)
        latencies_ms.append((time.perf_counter() - started) * 1000)
        if outcome.status != "verified":
            raise RuntimeError(f"{action.id} came back {outcome.status} ({outcome.verdict.rule}); nothing to measure")
    return {"n": n, **_summary(latencies_ms)}


async def against(
    url: str,
    key: str,
    *,
    rps: int,
    seconds: float,
    concurrency: int,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, float]:
    """Drive `rps` distinct writes per second for `seconds` at a gateway, at most `concurrency` in flight.

    `transport` replaces the network (tests pass an `httpx.ASGITransport` over an app).
    """
    if rps < 1 or seconds <= 0 or concurrency < 1:
        raise ValueError("rps and concurrency must be at least 1 and seconds positive")
    total = max(1, round(rps * seconds))
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    headers = {"Authorization": f"Bearer {key}"}
    async with httpx.AsyncClient(
        base_url=url, headers=headers, limits=limits, timeout=30.0, transport=transport
    ) as client:
        created = await client.post("/v1/sessions", json={"context": {"user_prompt": PROMPT}})
        created.raise_for_status()
        session_id = str(created.json()["session_id"])
        bodies = [{"session_id": session_id, "action": load_write(i).model_dump(mode="json")} for i in range(total)]
        slots = asyncio.Semaphore(concurrency)
        latencies_ms: list[float] = []
        service_ms: list[float] = []
        tally = {"ok": 0, "http_errors": 0, "transport_errors": 0}

        async def send(body: Mapping[str, object], due: float) -> None:
            async with slots:
                sent = time.perf_counter()
                try:
                    response = await client.post("/v1/execute", json=body)
                    outcome = "ok" if 200 <= response.status_code < 300 else "http_errors"
                except httpx.HTTPError:
                    outcome = "transport_errors"
                answered = time.perf_counter()
            latencies_ms.append((answered - due) * 1000)
            service_ms.append((answered - sent) * 1000)
            tally[outcome] += 1

        tasks: list[asyncio.Task[None]] = []
        start = time.perf_counter()
        for i, body in enumerate(bodies):
            due = start + i / rps
            delay = due - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            tasks.append(asyncio.create_task(send(body, due)))
        await asyncio.gather(*tasks)
        elapsed = time.perf_counter() - start
    return {
        "sent": total,
        "ok": tally["ok"],
        "errors": tally["http_errors"] + tally["transport_errors"],
        "http_errors": tally["http_errors"],
        "transport_errors": tally["transport_errors"],
        "achieved_rps": tally["ok"] / elapsed,
        **{name: value for name, value in _summary(latencies_ms).items() if name != "mean_ms"},
        "service_p50_ms": percentile(service_ms, 50),
        "service_p99_ms": percentile(service_ms, 99),
        "target_rps": rps,
        "seconds": seconds,
        "concurrency": concurrency,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure Benchpress write latency in process or against a gateway.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--in-process", type=int, metavar="N", help="time N verified writes through VerifiedWrite")
    mode.add_argument("--url", help="a running gateway's base URL, e.g. http://127.0.0.1:8798")
    parser.add_argument("--key", help="the gateway API key (required with --url)")
    parser.add_argument("--rps", type=int, default=500)
    parser.add_argument("--seconds", type=float, default=20.0)
    parser.add_argument("--concurrency", type=int, default=64)
    args = parser.parse_args(argv)
    if args.in_process is not None:
        result = asyncio.run(in_process(args.in_process))
    else:
        if not args.key:
            parser.error("--url needs --key")
        result = asyncio.run(
            against(args.url, args.key, rps=args.rps, seconds=args.seconds, concurrency=args.concurrency)
        )
    print(json.dumps({name: round(value, 3) for name, value in result.items()}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
