"""P4 — Plan: intents from the model, exact requests from playbooks, dry-run through the gate."""

from __future__ import annotations

from collections.abc import Sequence

from benchpress.context import Action, GateDecision, GateVerdict, Plan, utc_now
from benchpress.gate import GateRefusal
from benchpress.phases.common import PhaseDeps, dump, safe_emit
from benchpress.phases.schemas import ActionIntent, PlanDraft
from benchpress.prompts import render

MAX_WRITES = 24


def primitives_for(deps: PhaseDeps, providers: Sequence[str]) -> dict[str, list[str]]:
    described: dict[str, list[str]] = {}
    for provider in providers:
        if deps.playbook(provider) is None:
            continue
        ops = ["update(ref, fields) — change fields on a chosen record"]
        if provider == "slack":
            ops.append("message(channel, text) — controller-owned; do not plan")
        if provider == "gmail":
            ops.append("draft(to, subject, body) — controller-owned unsent draft; do not plan")
        described[provider] = ops
    return described


async def plan(deps: PhaseDeps) -> None:
    ctx = deps.ctx
    deps.bus.enter("P4")
    if ctx.ambiguous or not ctx.targets or not ctx.dod.end_state:
        if not ctx.ambiguous and not ctx.dod.end_state:
            deps.note("P4: no end-state items; nothing to write")
        ctx.plan = Plan()
        return
    draft = await safe_emit(
        deps,
        "plan",
        PlanDraft,
        render(
            "plan",
            frame=ctx.frame,
            dod=dump(ctx.dod),
            targets=[dump(target) for target in ctx.targets],
            protected=list(ctx.protected.all_terms()),
            primitives=primitives_for(deps, ctx.dod.write_scope),
        ),
        PlanDraft(),
    )
    actions = actions_from_intents(deps, draft.actions)
    covered = {item for action in actions for item in action.satisfies}
    for index, item in enumerate(ctx.dod.end_state):
        key = f"end_state[{index}]"
        if key in covered:
            continue
        playbook = deps.playbook(item.provider)
        target = ctx.target_for(item.provider)
        if playbook is None or target is None:
            continue
        synthesized = playbook.update_action(
            f"s{index}",
            item.resource,
            {item.field: item.expected},
            [key],
            target_refs=(item.resource, target.display),
            rationale="synthesized from the definition of done",
        )
        if synthesized is not None:
            actions.append(synthesized)
            covered.add(key)
    ctx.plan = Plan(actions=tuple(actions[:MAX_WRITES]))
    kept: list[Action] = []
    for action in ctx.plan.actions:
        try:
            deps.bus.gate.check(action)
        except GateRefusal as refusal:
            verdict = GateVerdict(action_id=action.id, allowed=False, rule=refusal.rule, reason=refusal.reason)
            ctx.gate_decisions.append(GateDecision(at=utc_now(), phase="P4", action=action, verdict=verdict))
            deps.note(f"P4: dropped {action.id} ({refusal.rule}: {refusal.reason})")
            continue
        kept.append(action)
    ctx.plan = Plan(actions=tuple(kept))


def actions_from_intents(deps: PhaseDeps, intents: Sequence[ActionIntent], *, prefix: str = "") -> list[Action]:
    ctx = deps.ctx
    targets = {(target.provider, target.ref): target for target in ctx.targets}
    actions: list[Action] = []
    for intent in intents:
        playbook = deps.playbook(intent.provider)
        if playbook is None:
            deps.note(f"P4: no playbook for {intent.provider}; intent {intent.id} dropped")
            continue
        if intent.kind != "update":
            deps.note(f"P4: {intent.kind} intents are controller-owned deliverables; {intent.id} dropped")
            continue
        target = targets.get((intent.provider, intent.ref))
        protected_ref = intent.ref.split(":", 1)[-1] in ctx.protected.ids
        if target is None and not protected_ref:
            deps.note(f"P4: intent {intent.id} targets a record that is not a chosen target; dropped")
            continue
        fields = {key: value for key, value in intent.fields.items() if key and value}
        if not fields or not intent.satisfies:
            continue
        # A write aimed at a protected record is built so the gate can refuse it on the record:
        # the refusal, with its rule, is part of the receipt.
        display = target.display if target is not None else intent.ref
        action = playbook.update_action(
            f"{prefix}{intent.id}",
            intent.ref,
            fields,
            intent.satisfies,
            target_refs=(intent.ref, display),
            rationale=intent.rationale,
        )
        if action is not None:
            actions.append(action)
    return actions
