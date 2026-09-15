"""Spans are emitted through the OpenTelemetry API only when the host process installed an SDK provider."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import benchpress.telemetry as telemetry
from benchpress.context import Context
from benchpress.loop import PHASE_ORDER, run_phases
from benchpress.telemetry import span
from benchpress.verified import VerifiedWrite
from tests.test_loop import _deps  # pyright: ignore[reportPrivateUsage]
from tests.test_verified import _PROMPT, FakeProvider, _write  # pyright: ignore[reportPrivateUsage]


@pytest.fixture
def spans(monkeypatch: pytest.MonkeyPatch) -> Iterator[InMemorySpanExporter]:
    """A `TracerProvider` local to this test, wired in by monkeypatching `_tracer` rather than by calling
    `trace.set_tracer_provider` globally — the real SDK global is process-wide and un-resettable,
    so setting it at import time would leak real spans into every later test in the session.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("benchpress")
    monkeypatch.setattr(telemetry, "_tracer", lambda: tracer)
    yield exporter


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


def test_span_emits_through_a_globally_installed_provider() -> None:
    """No monkeypatching here: a fresh process installs a real SDK provider globally, the way a host
    application would, and `span(...)`'s lazy `trace.get_tracer("benchpress")` call must reach it."""
    code = (
        "from opentelemetry import trace\n"
        "from opentelemetry.sdk.trace import TracerProvider\n"
        "from opentelemetry.sdk.trace.export import SimpleSpanProcessor\n"
        "from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter\n"
        "from benchpress.telemetry import span\n"
        "exporter = InMemorySpanExporter()\n"
        "provider = TracerProvider()\n"
        "provider.add_span_processor(SimpleSpanProcessor(exporter))\n"
        "trace.set_tracer_provider(provider)\n"
        "with span('x', a=1) as h:\n"
        "    h.set('b', 2)\n"
        "print(','.join(s.name for s in exporter.get_finished_spans()))\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert proc.stdout.strip() == "x"
