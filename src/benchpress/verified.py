"""`VerifiedWrite`: one write, gated in code, executed, read back, reduced to evidence.

This is the primitive every adapter and the gateway compose. It needs no model and no controller:
give it an executor with the harness `execute_tool(tool_name, tool_input)` shape and an `Action`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from benchpress.context import Action, Context, Evidence, GateVerdict
from benchpress.gate import Gate, PolicyRuleSet
from benchpress.phases.execute import fill_placeholders, readback_evidence
from benchpress.tools import BudgetExhausted, ToolBus, ToolExecutor, ToolResult

WriteStatus = Literal["refused", "failed", "unverified", "verified", "mismatch"]


@dataclass(frozen=True)
class WriteOutcome:
    action: Action
    verdict: GateVerdict
    result: ToolResult | None
    evidence: tuple[Evidence, ...]

    @property
    def status(self) -> WriteStatus:
        if not self.verdict.allowed:
            return "refused"
        if self.result is None or not self.result.ok:
            return "failed"
        if not self.evidence:
            return "unverified"
        return "verified" if all(item.match for item in self.evidence) else "mismatch"


class VerifiedWrite:
    """Gate -> execute -> read back -> evidence, for one action at a time.

    Stateful only in the way `Gate` is stateful: writes performed through the same
    `VerifiedWrite` are remembered, so a byte-identical replay is refused as `idempotency`
    rather than sent to the provider a second time.
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
        self._bus = ToolBus(context=self._context, execute=execute, gate=self._gate)

    @property
    def context(self) -> Context:
        """The context this write ran against — carries evidence and refusals for receipts."""
        return self._context

    async def run(self, action: Action) -> WriteOutcome:
        result, verdict = await self._bus.perform(action)
        if not verdict.allowed:
            return WriteOutcome(action, verdict, None, ())
        if not result.ok:
            return WriteOutcome(action, verdict, result, ())
        evidence = await self._readback(action, result.json())
        self._context.add_evidence(evidence)
        return WriteOutcome(action, verdict, result, evidence)

    async def _readback(self, action: Action, response_body: Mapping[str, object]) -> tuple[Evidence, ...]:
        spec = action.readback
        if spec is None:
            return ()
        path = fill_placeholders(spec.path, response_body)
        query = {key: fill_placeholders(value, response_body) for key, value in spec.query.items()}
        try:
            read = await self._bus.read(action.provider, path, query=query, method=spec.method, body=spec.body)
        except BudgetExhausted:
            return ()
        return tuple(readback_evidence(action, read))
