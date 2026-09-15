"""OpenTelemetry spans, emitted only through the API.

With no SDK configured by the host process, the API's default tracer drops every span, and without the
`opentelemetry` package at all, `span` is a no-op. Benchpress never configures an exporter and never sends anything
anywhere on its own.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

AttributeValue = str | int | float | bool


class SpanHandle:
    def __init__(self, span: Any | None) -> None:
        self._span = span

    def set(self, key: str, value: AttributeValue) -> None:
        if self._span is not None:
            self._span.set_attribute(key, value)


def _tracer() -> Any | None:
    try:
        from opentelemetry import trace
    except ImportError:
        return None
    return trace.get_tracer("benchpress")


@contextmanager
def span(name: str, **attributes: AttributeValue) -> Generator[SpanHandle]:
    tracer = _tracer()
    if tracer is None:
        yield SpanHandle(None)
        return
    with tracer.start_as_current_span(name, attributes=attributes) as current:
        yield SpanHandle(current)
