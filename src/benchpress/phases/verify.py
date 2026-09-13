"""P6 — Verify from state: end-state re-reads, cross-system presence, protected-set audit,
then one bounded repair round."""

from __future__ import annotations

import re

from benchpress.context import Evidence, Plan
from benchpress.normalize import canonical_email, contains_term
from benchpress.phases.common import PhaseDeps, dump, safe_emit
from benchpress.phases.execute import execute
from benchpress.phases.plan import actions_from_intents, primitives_for
from benchpress.phases.schemas import RepairPlan
from benchpress.prompts import render
from benchpress.tools import BudgetExhausted

_REPAIRABLE_PREFIXES = ("end_state", "readback", "write")


async def verify(deps: PhaseDeps) -> None:
    ctx = deps.ctx
    deps.bus.enter("P6")
    if ctx.ambiguous:
        return
    try:
        await check_end_state(deps)
        await check_cross_system(deps)
        await check_protected_unchanged(deps)
    except BudgetExhausted:
        deps.note("P6: budget exhausted during verification")


async def check_end_state(deps: PhaseDeps) -> None:
    ctx = deps.ctx
    for index, item in enumerate(ctx.dod.end_state):
        playbook = deps.playbook(item.provider)
        if playbook is None:
            continue
        observed = await playbook.read_field(deps.bus, item.resource, item.field)
        ctx.add_evidence(
            [
                Evidence(
                    check=f"end_state[{index}]",
                    provider=item.provider,
                    resource=item.resource,
                    expected=item.expected,
                    observed=observed or "",
                    match=compare(item.expected, observed, item.comparison),
                )
            ]
        )


async def check_cross_system(deps: PhaseDeps) -> None:
    ctx = deps.ctx
    if len(ctx.targets) < 2 or not ctx.dod.facts:
        return
    facts = [value for value in ctx.dod.facts.values() if value]
    providers_with_two = 0
    for target in ctx.targets:
        playbook = deps.playbook(target.provider)
        if playbook is None:
            continue
        record = await playbook.read_record(deps.bus, target.ref)
        text = " ".join(record.fields.values()) if record else ""
        present = sum(1 for value in facts if contains_term(text, value))
        if present >= 2:
            providers_with_two += 1
    ctx.add_evidence(
        [
            Evidence(
                check="cross_system",
                provider="*",
                resource="chosen targets",
                expected="at least 2 providers hold at least 2 facts",
                observed=f"{providers_with_two} provider(s)",
                match=providers_with_two >= 2,
            )
        ]
    )


async def check_protected_unchanged(deps: PhaseDeps) -> None:
    ctx = deps.ctx
    for key, before in ctx.candidate_snapshots.items():
        provider, ref = key.split(":", 1)
        playbook = deps.playbook(provider)
        if playbook is None:
            continue
        record = await playbook.read_record(deps.bus, ref)
        after = dict(record.fields) if record else {}
        changed = sorted(name for name, value in before.items() if after.get(name) != value)
        ctx.add_evidence(
            [
                Evidence(
                    check=f"protected_unchanged:{key}",
                    provider=provider,
                    resource=ref,
                    expected="byte-identical to the pre-run snapshot",
                    observed="unchanged" if not changed else f"changed fields: {', '.join(changed[:5])}",
                    match=not changed,
                )
            ]
        )


async def repair(deps: PhaseDeps) -> None:
    """One bounded repair round for failed end-state / read-back / write checks."""
    ctx = deps.ctx
    if ctx.ambiguous:
        return
    failed = [
        item
        for item in ctx.latest_evidence().values()
        if not item.match and item.check.startswith(_REPAIRABLE_PREFIXES)
    ]
    if not failed:
        return
    deps.bus.enter("repair")
    draft = await safe_emit(
        deps,
        "repair",
        RepairPlan,
        render(
            "repair",
            dod=dump(ctx.dod),
            evidence=[dump(item) for item in failed],
            refusals=[dump(item) for item in ctx.refusals],
            targets=[dump(target) for target in ctx.targets],
            primitives=primitives_for(deps, ctx.dod.write_scope),
        ),
        RepairPlan(),
    )
    actions = actions_from_intents(deps, draft.actions[:4], prefix="r-")
    if not actions:
        deps.note(f"repair: no safe repair actions ({draft.reason or 'none proposed'})")
        return
    ctx.plan = Plan(actions=(*ctx.plan.actions, *actions))
    await execute(deps, actions, phase="repair")
    try:
        await check_end_state(deps)
    except BudgetExhausted:
        deps.note("repair: budget exhausted before re-verification")


_MRKDWN_LINK = re.compile(r"<(?:mailto:|https?://)?([^<>|]+)(?:\|([^<>]+))?>")


def plain_text(value: str) -> str:
    """Undo Slack mrkdwn auto-links (`<mailto:a@b|a@b>`, `<http://x|x>`) so read-back compares what was written."""
    return _MRKDWN_LINK.sub(lambda m: m.group(2) or m.group(1), value)


def compare(expected: str, observed: str | None, comparison: str) -> bool:
    if observed is None:
        return False
    if comparison == "email":
        return canonical_email(expected) == canonical_email(observed)
    if comparison == "text":
        return plain_text(expected).strip().casefold() == plain_text(observed).strip().casefold()
    return contains_term(plain_text(observed), expected)
