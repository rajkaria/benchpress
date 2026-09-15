"""Spans are emitted through the OpenTelemetry API only when the host process installed an SDK provider."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from benchpress.context import Context
from benchpress.loop import PHASE_ORDER, run_phases
from benchpress.telemetry import span
from benchpress.verified import VerifiedWrite
from tests.test_loop import _deps  # pyright: ignore[reportPrivateUsage]
from tests.test_verified import _PROMPT, FakeProvider, _write  # pyright: ignore[reportPrivateUsage]

_EXPORTER = InMemorySpanExporter()
_PROVIDER = TracerProvider()
_PROVIDER.add_span_processor(SimpleSpanProcessor(_EXPORTER))
trace.set_tracer_provider(_PROVIDER)


@pytest.fixture(autouse=True)
def spans() -> Iterator[InMemorySpanExporter]:
    _EXPORTER.clear()
    yield _EXPORTER


def test_span_handle_sets_attributes(spans: InMemorySpanExporter) -> None:
    with span("benchpress.test", a=1) as handle:
        handle.set("b", "two")
    (finished,) = spans.get_finished_spans()
    assert finished.name == "benchpress.test" and dict(finished.attributes or {}) == {"a": 1, "b": "two"}


async def test_a_write_is_one_parent_span_with_execute_and_readback(spans: InMemorySpanExporter) -> None:
    provider = FakeProvider()
    await VerifiedWrite(provider.execute_tool, context=Context(user_prompt=_PROMPT)).run(_write())
    by_name = {s.name: s for s in spans.get_finished_spans()}
    write = by_name["benchpress.write"]
    attributes: dict[str, Any] = dict(write.attributes or {})
    assert attributes["benchpress.status"] == "verified" and attributes["benchpress.provider"] == "hubspot"
    write_context = write.context
    assert write_context is not None
    for child in ("benchpress.execute", "benchpress.readback"):
        parent = by_name[child].parent
        assert parent is not None and parent.span_id == write_context.span_id


async def test_every_loop_phase_is_a_span(spans: InMemorySpanExporter) -> None:
    from benchpress.demo import Workspace

    await run_phases(_deps(Workspace()))
    phases = [
        dict(s.attributes or {})["benchpress.phase"]
        for s in spans.get_finished_spans()
        if s.name == "benchpress.phase"
    ]
    assert phases == list(PHASE_ORDER)
