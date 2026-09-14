"""`VerifiedWrite`: one write, gated in code, executed, read back, reduced to evidence.

This is the primitive every adapter and the gateway compose. It needs no model and no controller:
give it an executor with the harness `execute_tool(tool_name, tool_input)` shape and an `Action`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from benchpress.context import Action, Context, Evidence, GateVerdict
from benchpress.gate import Gate, PolicyRuleSet, fingerprint
from benchpress.idempotency import IdempotencyStore, InMemoryIdempotencyStore
from benchpress.phases.execute import is_unreadable, perform_readback, readback_evidence, resource_of
from benchpress.tools import ToolBus, ToolExecutor, ToolResult

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
    ) -> None:
        self._context = context or Context()
        self._gate = Gate(self._context, allow_unplanned=allow_unplanned, policy_packs=policy_packs)
        self._bus = ToolBus(context=self._context, execute=execute, gate=self._gate, budgets=False)
        self._claims = idempotency or InMemoryIdempotencyStore()
        self._scope = scope

    @property
    def context(self) -> Context:
        """The context this write ran against — carries evidence and refusals for receipts."""
        return self._context

    async def run(self, action: Action) -> WriteOutcome:
        result, verdict = await self._perform_once(action)
        if not verdict.allowed:
            return WriteOutcome(action, verdict, None, ())
        if not result.ok:
            return WriteOutcome(action, verdict, result, ())
        evidence = await self._readback(action, result.json())
        self._context.add_evidence(evidence)
        return WriteOutcome(action, verdict, result, evidence)

    async def _perform_once(self, action: Action) -> tuple[ToolResult, GateVerdict]:
        """The gate first; then a claim on the store, so an identical write in flight or done anywhere is refused."""
        if not action.is_write or not self._gate.evaluate(action).allowed:
            return await self._bus.perform(action)
        key = fingerprint(action)
        claim = await self._claims.claim(self._scope, key)
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
            await self._claims.complete(self._scope, key, succeeded=result is not None and result.ok)

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
