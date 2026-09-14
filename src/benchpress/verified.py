"""`VerifiedWrite`: one write, gated in code, executed, read back, reduced to evidence.

This is the primitive every adapter and the gateway compose. It needs no model and no controller:
give it an executor with the harness `execute_tool(tool_name, tool_input)` shape and an `Action`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from benchpress.context import Action, Context, Evidence, GateVerdict
from benchpress.gate import Gate, PolicyRuleSet, fingerprint
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

        `mismatch` means a field was read back and contradicted the write. A read-back that could not be performed
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

    Stateful only in the way `Gate` is stateful: writes performed through the same `VerifiedWrite` are
    remembered, so a byte-identical replay is refused as `idempotency` rather than sent to the provider a
    second time. That includes concurrent calls: identical actions racing on one instance are serialized, the
    first is sent and the rest are refused as `idempotency`.

    * **One instance per logical session.** The idempotency memory lives in the instance. Two instances do not
      know about each other's writes, so do not share one `Context` across instances expecting them to
      de-duplicate: both would write.
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
    ) -> None:
        self._context = context or Context()
        self._gate = Gate(self._context, allow_unplanned=allow_unplanned, policy_packs=policy_packs)
        self._bus = ToolBus(context=self._context, execute=execute, gate=self._gate, budgets=False)
        self._inflight: dict[str, tuple[asyncio.Lock, int]] = {}

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
        """`bus.perform`, serialized per fingerprint so the gate's idempotency check sees an in-flight twin land."""
        key = fingerprint(action)
        lock, waiting = self._inflight.get(key, (asyncio.Lock(), 0))
        self._inflight[key] = (lock, waiting + 1)
        try:
            async with lock:
                return await self._bus.perform(action)
        finally:
            lock, waiting = self._inflight[key]
            if waiting <= 1:
                del self._inflight[key]
            else:
                self._inflight[key] = (lock, waiting - 1)

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
