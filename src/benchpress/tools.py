"""The tool bus — the single choke point for every provider call.

Nothing in Benchpress talks to `execute_tool` directly. Everything goes through here, so
that budgets, the ledger, the gate and the trace are impossible to bypass by accident.
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from benchpress.context import Action, Context, GateDecision, GateVerdict, LedgerEntry, utc_now
from benchpress.gate import (
    BLOCKED_EXACT_PATHS,
    BLOCKED_PATH_PREFIXES,
    Gate,
    GateRefusal,
    fingerprint,
)

ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[object]]

PROVIDER_API = "provider_api"
PROVIDER_DOCS = "provider_docs"

# ArgaBench harness limits per trial: 160 provider_api calls, 40 provider_docs calls, 1,800 s.
# Benchpress stays strictly under them.
MAX_PROVIDER_CALLS = 160
MAX_DOCS_CALLS = 40
MAX_WALL_SECONDS = 1_800.0

# Reserved so P6/P7 can always verify and deliver, no matter how P1/P2 went.
DELIVERY_RESERVE = 12

_REDACT_KEYS = frozenset({"authorization", "x-api-key", "api_key", "token", "secret", "password", "cookie"})


def as_mapping(value: object) -> Mapping[str, Any]:
    """Narrow an opaque tool response to a mapping without leaking Unknown into callers."""
    if isinstance(value, Mapping):
        return cast(Mapping[str, Any], value)
    return {}


class BudgetExhausted(RuntimeError):
    """Raised when a phase or the run has no calls left. Always caught; never fatal."""


@dataclass(frozen=True)
class PhaseBudget:
    provider_api: int
    provider_docs: int
    seconds: float


# Spec §10.4. Totals stay under 160/40/1650.
PHASE_BUDGETS: Mapping[str, PhaseBudget] = {
    "P0": PhaseBudget(6, 0, 60.0),
    "P1": PhaseBudget(30, 6, 300.0),
    "P2": PhaseBudget(40, 6, 300.0),
    "P3": PhaseBudget(0, 2, 60.0),
    "P4": PhaseBudget(0, 2, 60.0),
    "P5": PhaseBudget(40, 8, 420.0),
    "P6": PhaseBudget(20, 0, 180.0),
    "P7": PhaseBudget(12, 0, 120.0),
    "repair": PhaseBudget(12, 4, 150.0),
}


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    status_code: int | None
    body: object
    error: str | None = None

    def json(self) -> Mapping[str, Any]:
        return as_mapping(self.body)


@dataclass
class ToolBus:
    context: Context
    execute: ToolExecutor
    gate: Gate
    trace_path: str | None = None

    phase: str = "P0"
    started_at: float = field(default_factory=time.monotonic)
    provider_calls: int = 0
    docs_calls: int = 0
    _phase_provider: dict[str, int] = field(default_factory=dict[str, int])
    _phase_docs: dict[str, int] = field(default_factory=dict[str, int])
    _phase_started: dict[str, float] = field(default_factory=dict[str, float])
    _sequence: int = 0
    _events: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    _harness_events: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    # -- phase control -----------------------------------------------------------------

    def enter(self, phase: str) -> None:
        self.phase = phase
        self._phase_started.setdefault(phase, time.monotonic())

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def remaining_provider_calls(self) -> int:
        return max(0, MAX_PROVIDER_CALLS - self.provider_calls)

    def budget_left(self, *, docs: bool = False) -> int:
        budget = PHASE_BUDGETS.get(self.phase, PhaseBudget(0, 0, 0.0))
        if docs:
            return max(0, budget.provider_docs - self._phase_docs.get(self.phase, 0))
        used = self._phase_provider.get(self.phase, 0)
        phase_left = max(0, budget.provider_api - used)
        # Never spend the delivery reserve from an exploration phase.
        global_left = self.remaining_provider_calls()
        if self.phase not in {"P6", "P7"}:
            global_left = max(0, global_left - DELIVERY_RESERVE)
        return min(phase_left, global_left)

    def phase_seconds_left(self) -> float:
        budget = PHASE_BUDGETS.get(self.phase, PhaseBudget(0, 0, 0.0))
        started = self._phase_started.get(self.phase, time.monotonic())
        return max(0.0, budget.seconds - (time.monotonic() - started))

    def out_of_time(self) -> bool:
        return self.elapsed >= MAX_WALL_SECONDS or self.phase_seconds_left() <= 0.0

    # -- reads -------------------------------------------------------------------------

    async def read(
        self,
        provider: str,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
        method: Literal["GET", "POST"] = "GET",
        body: object | None = None,
        body_encoding: Literal["json", "form"] = "json",
    ) -> ToolResult:
        """A data-plane read. Never gated (reads cannot mutate), always budgeted."""
        if method == "POST" and body is None:
            body = {}
        return await self._call(
            provider=provider,
            method=method,
            path=path,
            query=dict(query or {}),
            body=body,
            body_encoding=body_encoding,
            action_id=None,
            gate_verdict=None,
        )

    async def docs(self, action: Literal["search", "fetch"], **kwargs: object) -> ToolResult:
        if self.budget_left(docs=True) <= 0 or self.docs_calls >= MAX_DOCS_CALLS:
            raise BudgetExhausted(f"{self.phase}: provider_docs budget exhausted")
        payload: dict[str, Any] = {"action": action, **kwargs}
        self.docs_calls += 1
        self._phase_docs[self.phase] = self._phase_docs.get(self.phase, 0) + 1
        started = time.monotonic()
        try:
            raw = await self.execute(PROVIDER_DOCS, payload)
        except Exception as exc:  # noqa: BLE001 - a docs failure must never end a run
            self._record_event("docs", payload, None, error=str(exc))
            self._record_harness_event(PROVIDER_DOCS, payload, {"error": {"type": type(exc).__name__}}, True, started)
            return ToolResult(ok=False, status_code=None, body=None, error=str(exc))
        self._record_event("docs", payload, raw)
        self._record_harness_event(PROVIDER_DOCS, payload, raw, False, started)
        return ToolResult(ok=True, status_code=200, body=raw)

    # -- writes ------------------------------------------------------------------------

    async def perform(self, action: Action) -> tuple[ToolResult, GateVerdict]:
        """Run one planned action through the gate. A refusal is a result, not an error."""
        try:
            verdict = self.gate.check(action)
        except GateRefusal as refusal:
            verdict = GateVerdict(
                action_id=action.id,
                allowed=False,
                rule=refusal.rule,
                reason=refusal.reason,
            )
            self.context.gate_decisions.append(
                GateDecision(at=utc_now(), phase=self.phase, action=action, verdict=verdict)
            )
            self._record_refusal(action, verdict)
            return ToolResult(ok=False, status_code=None, body=None, error=f"gate:{refusal.rule}"), verdict
        self.context.gate_decisions.append(GateDecision(at=utc_now(), phase=self.phase, action=action, verdict=verdict))

        result = await self._call(
            provider=action.provider,
            method=action.method,
            path=action.path,
            query=dict(action.query),
            body=action.body,
            body_encoding=action.body_encoding,
            headers=dict(action.headers),
            action_id=action.id,
            gate_verdict=verdict,
        )
        if result.ok:
            self.gate.record_success(fingerprint(action))
        return result, verdict

    # -- the one place a provider call actually happens --------------------------------

    async def _call(
        self,
        *,
        provider: str,
        method: str,
        path: str,
        query: dict[str, str],
        body: object | None,
        body_encoding: str,
        action_id: str | None,
        gate_verdict: GateVerdict | None,
        headers: dict[str, str] | None = None,
    ) -> ToolResult:
        blocked = _statically_blocked(path)
        if blocked:
            # Defense in depth: a blocked attempt is a hard unsafe at the gateway, so it
            # must never leave this process.
            result = ToolResult(ok=False, status_code=None, body=None, error=f"blocked_path:{blocked}")
            self._append_ledger(provider, method, path, "", result, gate_verdict, action_id)
            return result

        if self.budget_left() <= 0:
            raise BudgetExhausted(f"{self.phase}: provider_api budget exhausted")
        if self.provider_calls >= MAX_PROVIDER_CALLS:
            raise BudgetExhausted("global provider_api budget exhausted")

        payload: dict[str, Any] = {"provider": provider, "method": method, "path": path}
        if query:
            payload["query"] = query
        if body is not None:
            payload["body"] = body
            payload["body_encoding"] = body_encoding
        if headers:
            payload["headers"] = headers

        self.provider_calls += 1
        self._phase_provider[self.phase] = self._phase_provider.get(self.phase, 0) + 1

        started = time.monotonic()
        try:
            raw = await self.execute(PROVIDER_API, payload)
        except Exception as exc:  # noqa: BLE001 - transport failures are data, not crashes
            result = ToolResult(ok=False, status_code=None, body=None, error=str(exc))
            self._append_ledger(provider, method, path, _digest(payload), result, gate_verdict, action_id)
            self._record_event("api", payload, None, error=str(exc))
            self._record_harness_event(PROVIDER_API, payload, {"error": {"type": type(exc).__name__}}, True, started)
            return result

        result = _interpret(raw)
        self._append_ledger(provider, method, path, _digest(payload), result, gate_verdict, action_id)
        self._record_event("api", payload, raw)
        self._record_harness_event(PROVIDER_API, payload, raw, not result.ok, started)
        return result

    # -- bookkeeping -------------------------------------------------------------------

    def _append_ledger(
        self,
        provider: str,
        method: str,
        path: str,
        request_digest: str,
        result: ToolResult,
        gate_verdict: GateVerdict | None,
        action_id: str | None,
    ) -> None:
        self._sequence += 1
        self.context.ledger.append(
            LedgerEntry(
                sequence=self._sequence,
                phase=self.phase,
                provider=provider,
                method=method,
                path=path,
                fingerprint=request_digest,
                status_code=result.status_code,
                ok=result.ok,
                gate=gate_verdict,
                action_id=action_id,
                error=result.error,
                response_digest=_digest(result.body)[:16],
                at=utc_now(),
            )
        )

    def _record_refusal(self, action: Action, verdict: GateVerdict) -> None:
        self._sequence += 1
        self.context.ledger.append(
            LedgerEntry(
                sequence=self._sequence,
                phase=self.phase,
                provider=action.provider,
                method=action.method,
                path=action.path,
                fingerprint=fingerprint(action),
                status_code=None,
                ok=False,
                gate=verdict,
                action_id=action.id,
                error=f"gate:{verdict.rule}",
                at=utc_now(),
            )
        )
        self._record_event("gate_refusal", {"action": action.id, "rule": verdict.rule}, verdict.reason)

    def _record_event(self, kind: str, request: object, response: object, *, error: str | None = None) -> None:
        event: dict[str, Any] = {
            "sequence": self._sequence,
            "phase": self.phase,
            "kind": kind,
            "request": _redact(request),
            "response": _truncate(_redact(response)),
            "elapsed_s": round(self.elapsed, 3),
        }
        if error:
            event["error"] = error
        self._events.append(event)
        if self.trace_path:
            with open(self.trace_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")

    def _record_harness_event(
        self, name: str, arguments: dict[str, Any], output: object, is_error: bool, started: float
    ) -> None:
        """The shape ArgaBench's own adapters emit per tool call; `output` is
        the executor's dict verbatim so trace sequence and fingerprints survive."""
        index = len(self._harness_events) + 1
        self._harness_events.append(
            {
                "type": "tool_call",
                "provider_call_index": index,
                "tool_use_id": f"bp-{index}",
                "name": name,
                "arguments": dict(arguments),
                "output": output,
                "is_error": is_error,
                "latency_ms": round((time.monotonic() - started) * 1000),
            }
        )

    @property
    def events(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._events)

    @property
    def harness_events(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._harness_events)

    def usage(self) -> dict[str, Any]:
        return {
            "provider_api_calls": self.provider_calls,
            "provider_docs_calls": self.docs_calls,
            "elapsed_s": round(self.elapsed, 2),
            "per_phase_provider_api": dict(self._phase_provider),
            "per_phase_provider_docs": dict(self._phase_docs),
            "gate_refusals": len(self.context.refusals),
        }


# --------------------------------------------------------------------------------------


def _statically_blocked(path: str) -> str | None:
    candidate = path if path.startswith("/") else f"/{path}"
    lowered = candidate.split("?", 1)[0].casefold()
    if "://" in path:
        return "absolute_url"
    if lowered in BLOCKED_EXACT_PATHS:
        return lowered
    for prefix in BLOCKED_PATH_PREFIXES:
        if lowered == prefix or lowered.startswith(f"{prefix}/"):
            return prefix
    return None


def _interpret(raw: object) -> ToolResult:
    """The gateway returns a mapping describing the upstream response."""
    if not isinstance(raw, Mapping):
        return ToolResult(ok=True, status_code=200, body=raw)
    payload = dict(cast(Mapping[str, object], raw))
    status = payload.get("status_code", payload.get("status"))
    code = status if isinstance(status, int) else None
    body = payload.get("body", payload.get("data", payload))
    error = payload.get("error")
    ok = code is None or 200 <= code < 300
    if error and not ok:
        return ToolResult(ok=False, status_code=code, body=body, error=str(error))
    return ToolResult(ok=ok, status_code=code, body=body, error=str(error) if error else None)


def _redact(value: object) -> object:
    if isinstance(value, Mapping):
        out: dict[str, object] = {}
        for raw_key, raw_value in cast(Mapping[object, object], value).items():
            key = str(raw_key)
            out[key] = "<redacted>" if key.casefold() in _REDACT_KEYS else _redact(raw_value)
        return out
    if isinstance(value, list):
        return [_redact(item) for item in cast(list[object], value)]
    return value


def _truncate(value: object, limit: int = 4_000) -> object:
    rendered = json.dumps(value, ensure_ascii=False, default=str)
    if len(rendered) <= limit:
        return value
    return {"_truncated": True, "preview": rendered[:limit]}


def _digest(value: object) -> str:
    import hashlib

    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:32]
