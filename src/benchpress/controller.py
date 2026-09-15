"""The Benchpress controller: fixed phase order, typed context, code-owned safety.

    P0 orient → P1 policy sweep → P2 enumerate + resolve → P3 definition of done → P4 plan
    → P5 execute through the gate + read-back → P6 verify (+ one repair round) → P7 deliver

P7 always runs, with whatever evidence exists, so the run always ends with an honest,
evidence-only structured result.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from benchpress.context import Action, Context, GateVerdict
from benchpress.gate import Gate, PolicyRuleSet
from benchpress.loop import run_phases
from benchpress.model import ModelClient, ModelConfig, UsageTotals
from benchpress.phases.common import Ablations, PhaseDeps, resolve_playbooks
from benchpress.phases.deliver import deliver, final_json
from benchpress.playbooks import Playbook
from benchpress.prompts import system_text
from benchpress.report import receipt_payload, write_receipt
from benchpress.tools import BudgetExhausted, ToolBus, ToolExecutor

PHASES_TIMEOUT_S = 1_720.0
DELIVER_TIMEOUT_S = 150.0


def _pack_ref(pack: object) -> str:
    """How a replay finds the pack again: its bundled name, or the file it was loaded from."""
    from benchpress.packs import POLICY_PACKS_DIR

    name = str(getattr(pack, "name", "") or type(pack).__name__)
    source = str(getattr(pack, "source", "") or "")
    if source and Path(source).resolve().parent != POLICY_PACKS_DIR.resolve():
        return str(Path(source).resolve())
    return name


class AuditingGate(Gate):
    """The `no_gate` ablation: allow every write, but record what the gate would have refused."""

    def check(self, action: Action) -> GateVerdict:
        verdict = self.evaluate(action)
        if not verdict.allowed:
            self.context.would_refuse.append(verdict)
        return GateVerdict(action_id=action.id, allowed=True, rule="ablated")


@dataclass(frozen=True)
class TrialResult:
    final_text: str
    status: Literal["completed", "partial", "escalated"]
    context: Context
    tool_events: tuple[dict[str, Any], ...]
    model_events: tuple[dict[str, Any], ...]
    usage: UsageTotals
    provider_calls: int
    docs_calls: int
    latency_ms: int
    cost_usd: float
    ablations: tuple[str, ...]
    error: str | None = None

    def meta(self, config: ModelConfig) -> dict[str, Any]:
        return {
            "model": config.model,
            "model_provider": config.provider,
            "effort": config.effort,
            "provider_calls": self.provider_calls,
            "docs_calls": self.docs_calls,
            "model_calls": self.usage.calls,
            "latency_ms": self.latency_ms,
            "cost_usd": self.cost_usd,
            "usage": self.usage.harness_usage(),
            "ablations": list(self.ablations),
            "error": self.error,
        }


async def run_trial(
    *,
    system_prompt: str,
    user_prompt: str,
    providers: Sequence[str],
    execute_tool: ToolExecutor,
    config: ModelConfig | None = None,
    ablations: Ablations | None = None,
    trace_dir: Path | None = None,
    trial_id: str | None = None,
    model_client: ModelClient | None = None,
    playbooks: Mapping[str, Playbook] | None = None,
    policy_packs: Sequence[PolicyRuleSet] | None = None,
) -> TrialResult:
    started = time.monotonic()
    ablations = ablations or Ablations()
    config = config or ModelConfig.from_env()
    ctx = Context(
        trial_id=trial_id or f"bp-{uuid.uuid4().hex[:8]}",
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        providers=tuple(providers),
    )
    packs = tuple(policy_packs or ())
    ctx.policy_packs = tuple(_pack_ref(pack) for pack in packs)
    gate = AuditingGate(ctx, policy_packs=packs) if ablations.no_gate else Gate(ctx, policy_packs=packs)
    trace_path: str | None = None
    if trace_dir is not None:
        trace_dir.mkdir(parents=True, exist_ok=True)
        trace_path = str(trace_dir / "benchpress-trace.jsonl")
    bus = ToolBus(context=ctx, execute=execute_tool, gate=gate, trace_path=trace_path)
    model = model_client or ModelClient(config, system_text(system_prompt))
    deps = PhaseDeps(
        ctx=ctx,
        model=model,
        bus=bus,
        ablations=ablations,
        playbooks=dict(playbooks) if playbooks is not None else resolve_playbooks(providers),
    )
    error: str | None = None
    try:
        async with asyncio.timeout(PHASES_TIMEOUT_S):
            await run_phases(deps)
    except TimeoutError:
        error = "timeout"
        ctx.notes.append("controller: phase budget exhausted (timeout); delivering with the evidence so far")
    except BudgetExhausted as exc:
        error = f"budget: {exc}"
        ctx.notes.append(f"controller: {exc}; delivering with the evidence so far")
    except Exception as exc:  # noqa: BLE001 - a crash must still end in an honest report
        error = f"{type(exc).__name__}: {exc}"
        ctx.notes.append(f"controller: unexpected error {error}; delivering with the evidence so far")
    try:
        async with asyncio.timeout(DELIVER_TIMEOUT_S):
            final_text = await deliver(deps)
    except Exception as exc:  # noqa: BLE001
        ctx.notes.append(f"controller: delivery failed ({type(exc).__name__}: {exc})")
        final_text = final_json(ctx)
    latency = round((time.monotonic() - started) * 1000)
    result = TrialResult(
        final_text=final_text,
        status=ctx.status(),
        context=ctx,
        tool_events=bus.harness_events,
        model_events=tuple(model.events),
        usage=model.usage,
        provider_calls=bus.provider_calls,
        docs_calls=bus.docs_calls,
        latency_ms=latency,
        cost_usd=model.usage.cost_usd(config.pricing),
        ablations=ablations.active(),
        error=error,
    )
    if trace_dir is not None:
        write_receipt(trace_dir / "receipt.json", receipt_payload(ctx, meta=result.meta(config)))
    return result
