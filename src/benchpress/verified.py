"""`VerifiedWrite`: one write, gated in code, executed, read back, reduced to evidence.

This is the primitive every adapter and the gateway compose. It needs no model and no controller:
give it an executor with the harness `execute_tool(tool_name, tool_input)` shape and an `Action`.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from benchpress.context import Action, Context, Evidence, GateVerdict, utc_now
from benchpress.gate import Gate, PolicyRuleSet, fingerprint
from benchpress.idempotency import IdempotencyStore, InMemoryIdempotencyStore
from benchpress.phases.execute import is_unreadable, perform_readback, readback_evidence, resource_of
from benchpress.telemetry import span
from benchpress.tools import ToolBus, ToolExecutor, ToolResult
from benchpress.write_receipts import Clock, ReceiptSink

WriteStatus = Literal["refused", "failed", "unverified", "verified", "mismatch"]


@dataclass(frozen=True)
class WriteOutcome:
    action: Action
    verdict: GateVerdict
    result: ToolResult | None
    evidence: tuple[Evidence, ...]

    @property
    def status(self) -> WriteStatus:
        """Computed from evidence only.

        `mismatch` means a declared field was read back and contradicted the write, or the read-back succeeded
        without showing it (`observed="missing"`). A read-back that could not be performed
        or read (skipped, non-2xx, transport error) proves nothing either way, so it is `unverified`; its evidence
        is kept on the outcome so the reason is visible.
        """
        if not self.verdict.allowed:
            return "refused"
        if self.result is None or not self.result.ok:
            return "failed"
        observed = [item for item in self.evidence if not is_unreadable(self.action, item)]
        if any(not item.match for item in observed):
            return "mismatch"
        if not observed or len(observed) != len(self.evidence):
            return "unverified"
        return "verified"


class VerifiedWrite:
    """Gate -> execute -> read back -> evidence, for one action at a time.

    De-duplication spans every `VerifiedWrite` instance that shares an `IdempotencyStore` and `scope`: a
    byte-identical write already done anywhere on that store is refused as `idempotency` rather than sent to the
    provider again, and a byte-identical write already in flight on another instance (or another gateway
    replica) is refused rather than queued behind it. By default each instance gets its own private
    `InMemoryIdempotencyStore`, matching the single-instance behaviour of a lone writer.

    * **Share the store to de-duplicate across instances.** Pass the same `idempotency` (and `scope`) to every
      `VerifiedWrite` that should see each other's claims — e.g. every replica behind one gateway. Two instances
      with different stores, or different scopes on the same store, do not know about each other's writes.
    * **A mismatch is still a write.** A write that returned 2xx but read back as `mismatch` is recorded as done,
      so replaying the identical action is refused. Fix the cause and send a different action.
    * **Not a benchmark trial.** The tool bus runs without the controller's per-phase budgets or lifetime call
      cap, so a long-lived instance never runs out of calls and `run()` never raises `BudgetExhausted`.
    * **History is kept by default.** The context and the tool bus record every write (gate decisions, refusals,
      ledger, evidence, events). A long-lived instance passes `history_limit` to keep only the newest N of each once
      a `run` has built its outcome and emitted its receipt; no verdict, outcome or receipt reads those records.
    """

    def __init__(
        self,
        execute: ToolExecutor,
        *,
        context: Context | None = None,
        policy_packs: Sequence[PolicyRuleSet] = (),
        allow_unplanned: bool = True,
        idempotency: IdempotencyStore | None = None,
        scope: str = "default",
        receipts: ReceiptSink | None = None,
        clock: Clock = utc_now,
        workspace: str = "local",
        session: str | None = None,
        history_limit: int | None = None,
    ) -> None:
        if history_limit is not None and history_limit < 0:
            raise ValueError("history_limit must be a non-negative integer or None")
        self._context = context or Context()
        self._gate = Gate(self._context, allow_unplanned=allow_unplanned, policy_packs=policy_packs)
        self._bus = ToolBus(context=self._context, execute=execute, gate=self._gate, budgets=False)
        self._claims = idempotency or InMemoryIdempotencyStore()
        self._scope = scope
        self._receipts = receipts
        self._clock = clock
        self._workspace = workspace
        self._session = session
        self._history_limit = history_limit

    @property
    def context(self) -> Context:
        """The context this write ran against — carries evidence and refusals for receipts."""
        return self._context

    def evaluate(self, action: Action) -> GateVerdict:
        """The gate's verdict on `action`, without executing it, recording a refusal or taking an idempotency claim.

        Only `run` consults the shared claims, so a write already done by another instance can still evaluate as
        allowed here; `run` would refuse it as `idempotency`.
        """
        return self._gate.evaluate(action)

    async def run(self, action: Action) -> WriteOutcome:
        with span(
            "benchpress.write",
            **{
                "benchpress.provider": action.provider,
                "benchpress.method": action.method,
                "benchpress.action_id": action.id,
            },
        ) as handle:
            outcome = await self._run_body(action)
            handle.set("benchpress.status", outcome.status)
            handle.set("benchpress.rule", outcome.verdict.rule)
            return outcome

    async def _run_body(self, action: Action) -> WriteOutcome:
        with span("benchpress.execute"):
            result, verdict = await self._perform_once(action)
        if not verdict.allowed:
            return await self._finish(WriteOutcome(action, verdict, None, ()))
        if not result.ok:
            return await self._finish(WriteOutcome(action, verdict, result, ()))
        with span("benchpress.readback"):
            evidence = await self._readback(action, result.json())
        self._context.add_evidence(evidence)
        return await self._finish(WriteOutcome(action, verdict, result, evidence))

    async def _finish(self, outcome: WriteOutcome) -> WriteOutcome:
        if self._receipts is not None:
            from benchpress.write_receipts import write_line

            line = write_line(
                outcome,
                at=self._clock(),
                workspace=self._workspace,
                session=self._session or self._context.trial_id,
            )
            await self._receipts.emit(line)
        self._trim_history()
        return outcome

    def _trim_history(self) -> None:
        if self._history_limit is None:
            return
        ctx = self._context
        for records in (ctx.gate_decisions, ctx.refusals, ctx.ledger, ctx.evidence):
            del records[: max(0, len(records) - self._history_limit)]
        self._bus.trim_history(self._history_limit)

    async def _perform_once(self, action: Action) -> tuple[ToolResult, GateVerdict]:
        """The gate first; then a claim on the store, so an identical write in flight or done anywhere is refused."""
        if not action.is_write or not self._gate.evaluate(action).allowed:
            return await self._bus.perform(action)
        key = fingerprint(action)
        holder = uuid.uuid4().hex
        claim = await self._claims.claim(self._scope, key, holder=holder)
        if claim != "claimed":
            reason = (
                f"action {action.id!r} already succeeded; replay would duplicate"
                if claim == "done"
                else f"an identical write for action {action.id!r} is already in flight"
            )
            verdict = GateVerdict(action_id=action.id, allowed=False, rule="idempotency", reason=reason)
            return self._bus.refuse(action, verdict)
        result: ToolResult | None = None
        try:
            result, verdict = await self._bus.perform(action)
            return result, verdict
        finally:
            await self._claims.complete(self._scope, key, holder=holder, succeeded=result is not None and result.ok)

    async def _readback(self, action: Action, response_body: Mapping[str, object]) -> tuple[Evidence, ...]:
        if action.readback is None:
            return ()
        read = await perform_readback(self._bus, action, response_body)
        if read is None:
            return (
                Evidence(
                    check=f"readback:{action.id}",
                    provider=action.provider,
                    resource=resource_of(action),
                    expected="readable",
                    observed="skipped: the tool bus had no budget left for the read-back",
                    match=False,
                ),
            )
        return tuple(readback_evidence(action, read))
