"""Prometheus metrics for the gateway: one `CollectorRegistry` per app, never the global default.

The core import guard excludes `benchpress.gateway`, so this module may import `prometheus_client` at the top
level — `import benchpress` alone never pulls it in, only `import benchpress.gateway...` does.
"""

from __future__ import annotations

from collections.abc import Sequence

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Histogram, generate_latest

from benchpress.playbooks import available_providers
from benchpress.verified import WriteOutcome

__all__ = ["GatewayMetrics", "create_metrics"]


class GatewayMetrics:
    """Every gateway metric, on its own registry so several apps in one process never collide.

    Ruling R15: a provider is a label value only if it is bundled (a playbook provider) or configured (an
    upstream) — `known_providers`. Anything else is a free string straight from an `/v1/execute` body, so it
    is labelled `"other"` instead of letting a caller mint unbounded (including histogram) series.
    """

    def __init__(self, *, known_providers: Sequence[str] = ()) -> None:
        self._known_providers = frozenset(known_providers)
        self.registry = CollectorRegistry()
        self.writes = Counter(
            "benchpress_writes_total",
            "Writes completed, by provider and outcome status",
            ["provider", "status"],
            registry=self.registry,
        )
        self.refusals = Counter(
            "benchpress_refusals_total",
            "Refused writes, by the rule that refused them",
            ["rule"],
            registry=self.registry,
        )
        self.readback_mismatches = Counter(
            "benchpress_readback_mismatches_total",
            "Writes whose read-back contradicted them, by provider",
            ["provider"],
            registry=self.registry,
        )
        self.approvals = Counter(
            "benchpress_approvals_total",
            "Approval queue events: requested, approved, denied, expired",
            ["event"],
            registry=self.registry,
        )
        self.write_seconds = Histogram(
            "benchpress_write_seconds",
            "Time to run one write end to end, by provider",
            ["provider"],
            registry=self.registry,
        )
        self.http_requests = Counter(
            "benchpress_http_requests_total",
            "HTTP requests, by route template, method and status code",
            ["route", "method", "code"],
            registry=self.registry,
        )
        self.http_seconds = Histogram(
            "benchpress_http_request_seconds",
            "HTTP request duration in seconds, by route template",
            ["route"],
            registry=self.registry,
        )

    def _provider_label(self, provider: str) -> str:
        """`provider` itself if it is bundled or configured, else `"other"` (Ruling R15)."""
        return provider if provider in self._known_providers else "other"

    def observe_outcome(self, outcome: WriteOutcome, seconds: float) -> None:
        """Update every write-shaped metric for one finished `VerifiedWrite.run`, whatever its status."""
        provider = self._provider_label(outcome.action.provider)
        self.writes.labels(provider=provider, status=outcome.status).inc()
        if outcome.status == "refused":
            self.refusals.labels(rule=outcome.verdict.rule).inc()
        if outcome.status == "mismatch":
            self.readback_mismatches.labels(provider=provider).inc()
        self.write_seconds.labels(provider=provider).observe(seconds)

    def render(self) -> tuple[bytes, str]:
        """The current metrics in Prometheus text exposition format, with their content type."""
        return generate_latest(self.registry), CONTENT_TYPE_LATEST


def create_metrics(*, extra_providers: Sequence[str] = ()) -> GatewayMetrics:
    """`extra_providers` are configured upstreams (`settings.upstreams`), added to the bundled playbook set."""
    return GatewayMetrics(known_providers=(*available_providers(), *extra_providers))
