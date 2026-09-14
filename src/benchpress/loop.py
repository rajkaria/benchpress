"""The loop's phases, importable one at a time. `run_trial` is their composition.

    deps = build_deps(request, executor, providers=["hubspot"], model=client)
    targets = await resolve_targets(deps)      # runs orient and the policy sweep first
    status = await verify(deps)                # runs every remaining phase through repair

Every function runs the phases before it that have not run yet, in `PHASE_ORDER`, so calling one directly can never
skip the policy sweep or planning. A phase never runs twice on the same `deps`.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence

from benchpress.context import (
    Context,
    DefinitionOfDone,
    Evidence,
    Plan,
    PolicyRecord,
    ResolvedTarget,
    RunStatus,
    TaskFrame,
)
from benchpress.gate import Gate, PolicyRuleSet
from benchpress.model import ModelClient
from benchpress.phases.common import Ablations, PhaseDeps, resolve_playbooks
from benchpress.phases.deliver import deliver as _deliver
from benchpress.phases.dod import define_done as _define_done
from benchpress.phases.execute import execute as _execute
from benchpress.phases.orient import orient as _orient
from benchpress.phases.plan import plan as _plan
from benchpress.phases.policy import policy_sweep as _policy_sweep
from benchpress.phases.resolve import resolve as _resolve
from benchpress.phases.verify import repair as _repair
from benchpress.phases.verify import verify as _verify
from benchpress.playbooks import Playbook
from benchpress.tools import ToolBus, ToolExecutor

PHASE_ORDER: tuple[str, ...] = (
    "orient",
    "policy_sweep",
    "resolve",
    "define_done",
    "plan",
    "execute",
    "verify",
    "repair",
)

_PHASES: dict[str, Callable[[PhaseDeps], Awaitable[None]]] = {
    "orient": _orient,
    "policy_sweep": _policy_sweep,
    "resolve": _resolve,
    "define_done": _define_done,
    "plan": _plan,
    "execute": _execute,
    "verify": _verify,
    "repair": _repair,
}


def build_deps(
    request: str,
    execute_tool: ToolExecutor,
    *,
    providers: Sequence[str],
    model: ModelClient,
    playbooks: Mapping[str, Playbook] | None = None,
    policy_packs: Sequence[PolicyRuleSet] = (),
    ablations: Ablations | None = None,
    trial_id: str | None = None,
    system_prompt: str = "",
    trace_path: str | None = None,
) -> PhaseDeps:
    """A fresh context, gate and bounded tool bus for one request: what `run_trial` builds, minus the timeouts."""
    ctx = Context(
        trial_id=trial_id or f"bp-{uuid.uuid4().hex[:8]}",
        system_prompt=system_prompt,
        user_prompt=request,
        providers=tuple(providers),
    )
    gate = Gate(ctx, policy_packs=tuple(policy_packs))
    bus = ToolBus(context=ctx, execute=execute_tool, gate=gate, trace_path=trace_path)
    return PhaseDeps(
        ctx=ctx,
        model=model,
        bus=bus,
        ablations=ablations or Ablations(),
        playbooks=dict(playbooks) if playbooks is not None else resolve_playbooks(providers),
    )


async def _run_through(deps: PhaseDeps, last: str) -> None:
    for name in PHASE_ORDER[: PHASE_ORDER.index(last) + 1]:
        if name in deps.completed:
            continue
        await _PHASES[name](deps)
        deps.completed.append(name)


async def orient_request(deps: PhaseDeps) -> TaskFrame:
    await _run_through(deps, "orient")
    return deps.ctx.frame


async def policy_sweep(deps: PhaseDeps) -> list[PolicyRecord]:
    await _run_through(deps, "policy_sweep")
    return list(deps.ctx.policies)


async def resolve_targets(deps: PhaseDeps) -> list[ResolvedTarget]:
    await _run_through(deps, "resolve")
    return list(deps.ctx.targets)


async def definition_of_done(deps: PhaseDeps) -> DefinitionOfDone:
    await _run_through(deps, "define_done")
    return deps.ctx.dod


async def plan_actions(deps: PhaseDeps) -> Plan:
    await _run_through(deps, "plan")
    return deps.ctx.plan


async def execute_plan(deps: PhaseDeps) -> list[Evidence]:
    before = len(deps.ctx.evidence)
    await _run_through(deps, "execute")
    return deps.ctx.evidence[before:]


async def verify(deps: PhaseDeps) -> RunStatus:
    """Verify the end state and run the one bounded repair round; the status is computed from evidence."""
    await _run_through(deps, "repair")
    return deps.ctx.status()


async def run_phases(deps: PhaseDeps) -> None:
    await _run_through(deps, PHASE_ORDER[-1])


async def deliver(deps: PhaseDeps) -> str:
    return await _deliver(deps)


__all__ = [
    "PHASE_ORDER",
    "build_deps",
    "definition_of_done",
    "deliver",
    "execute_plan",
    "orient_request",
    "plan_actions",
    "policy_sweep",
    "resolve_targets",
    "run_phases",
    "verify",
]
