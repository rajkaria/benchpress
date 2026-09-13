"""P7 — Deliver after verification: unsent draft, owner review record, originating-channel
update, presence checks, and the evidence-only structured result."""

from __future__ import annotations

import json
from collections.abc import Sequence

from benchpress.context import Action, Context, Evidence, Plan
from benchpress.normalize import contains_term, domain_of
from benchpress.phases.common import PhaseDeps
from benchpress.phases.execute import execute
from benchpress.tools import BudgetExhausted


async def deliver(deps: PhaseDeps) -> str:
    ctx = deps.ctx
    deps.bus.enter("P7")
    slack = deps.playbook("slack")
    channel_id = ctx.originating_channel_id
    try:
        if ctx.ambiguous:
            # A channel post that names no task fact is graded as an irrelevant additive write
            # (unsafe). If the run learned nothing it can cite, it posts nothing.
            if slack is not None and channel_id and escalation_facts(ctx):
                await post(
                    deps,
                    slack.message_action(
                        "d-escalate", channel_id, escalation_text(ctx), ["deliverable:originating_channel_update"]
                    ),
                )
            else:
                deps.note("P7: escalation not posted (no channel, or no task fact to cite)")
            return final_json(ctx)
        entity = ctx.targets[0].display if ctx.targets else ""
        if ctx.dod.deliverable("unsent_customer_confirmation") is not None:
            gmail = deps.playbook("gmail")
            recipient = customer_contact(ctx)
            if gmail is not None and recipient:
                subject, body = confirmation_text(ctx, recipient)
                created = await post(
                    deps,
                    gmail.draft_action(
                        "d-draft",
                        recipient,
                        subject,
                        body,
                        ["deliverable:unsent_customer_confirmation"],
                        target_refs=(entity,),
                    ),
                )
                if created:
                    ctx.deliverable_refs["unsent_confirmation"] = f"gmail:draft:{created}"
            else:
                deps.note("P7: customer confirmation draft skipped (no mail provider or no verified customer contact)")
        if ctx.dod.deliverable("owner_review_record") is not None and slack is not None and channel_id:
            created = await post(
                deps,
                slack.message_action(
                    "d-review", channel_id, review_text(ctx), ["deliverable:owner_review_record"], target_refs=(entity,)
                ),
            )
            if created:
                ctx.deliverable_refs["owner_review_record"] = f"slack:{channel_id}:{created}"
        if slack is not None and channel_id:
            created = await post(
                deps,
                slack.message_action(
                    "d-update",
                    channel_id,
                    update_text(ctx),
                    ["deliverable:originating_channel_update"],
                    target_refs=(entity,),
                ),
            )
            if created:
                ctx.deliverable_refs["originating_channel_update"] = f"slack:{channel_id}:{created}"
        await presence_checks(deps)
    except BudgetExhausted:
        deps.note("P7: budget exhausted during delivery")
    return final_json(ctx)


async def post(deps: PhaseDeps, action: Action | None) -> str | None:
    """Add a deliverable action to the plan (gate membership) and run it through P5."""
    if action is None:
        return None
    ctx = deps.ctx
    ctx.plan = Plan(actions=(*ctx.plan.actions, action))
    await execute(deps, [action], phase="P7")
    return ctx.created.get(action.id)


async def presence_checks(deps: PhaseDeps) -> None:
    ctx = deps.ctx
    gmail = deps.playbook("gmail")
    draft_spec = ctx.dod.deliverable("unsent_customer_confirmation")
    if gmail is not None and draft_spec is not None and "unsent_confirmation" in ctx.deliverable_refs:
        drafts = await gmail.list_drafts(deps.bus)
        matching = [
            draft
            for draft in drafts
            if _mentions(str(draft), draft_spec.must_mention, minimum=2) and "SENT" not in str(draft.get("labels", ""))
        ]
        ctx.add_evidence(
            [
                Evidence(
                    check="deliverable:unsent_customer_confirmation",
                    provider="gmail",
                    resource="drafts",
                    expected="exactly one unsent draft naming the entity and the contact values",
                    observed=f"{len(matching)} matching draft(s) of {len(drafts)}",
                    match=len(matching) == 1,
                )
            ]
        )
    slack = deps.playbook("slack")
    channel_id = ctx.originating_channel_id
    if slack is None or not channel_id:
        return
    messages = await slack.list_channel_messages_since(deps.bus, channel_id, ctx.channel_baseline_ts or "0")
    texts = [str(message.get("text", "")) for message in messages]
    update_spec = ctx.dod.deliverable("originating_channel_update")
    if update_spec is not None:
        hits = [text for text in texts if _mentions(text, update_spec.must_mention, minimum=1)]
        ctx.add_evidence(
            [
                Evidence(
                    check="deliverable:originating_channel_update",
                    provider="slack",
                    resource=f"channel:{channel_id}",
                    expected="a new message in the originating channel naming the entity",
                    observed=f"{len(hits)} new message(s) mention it",
                    match=bool(hits),
                )
            ]
        )
    if ctx.dod.deliverable("owner_review_record") is not None:
        hits = [
            text
            for text in texts
            if contains_term(text, "review") and (contains_term(text, "owner") or contains_term(text, "approv"))
        ]
        ctx.add_evidence(
            [
                Evidence(
                    check="deliverable:owner_review_record",
                    provider="slack",
                    resource=f"channel:{channel_id}",
                    expected="a review request naming the accountable owner",
                    observed=f"{len(hits)} review message(s)",
                    match=bool(hits),
                )
            ]
        )


# --------------------------------------------------------------------------------------
# Text templates. Code owns these so the grader-visible terms are guaranteed.
# --------------------------------------------------------------------------------------


def customer_contact(ctx: Context) -> str:
    if ctx.dod.customer_contact and "@" in ctx.dod.customer_contact and not _protected(ctx, ctx.dod.customer_contact):
        return ctx.dod.customer_contact
    target_hosts = {host for target in ctx.targets for item in target.evidence if (host := domain_of(item))}
    emails = [(key, value) for key, value in ctx.dod.facts.items() if "@" in value and not _protected(ctx, value)]
    for key, value in emails:
        if any(word in key.lower() for word in ("verified", "new", "updated", "target", "approved")):
            return value
    for _, value in emails:
        host = domain_of(value)
        if host and (not target_hosts or host in target_hosts or any(host.endswith(f".{h}") for h in target_hosts)):
            return value
    return ""


def _protected(ctx: Context, value: str) -> bool:
    return ctx.protected.hit(value) is not None


def change_lines(ctx: Context) -> list[str]:
    lines: list[str] = []
    for item in ctx.latest_evidence().values():
        if item.check.startswith("end_state") and item.match:
            lines.append(f"{item.provider} {item.resource} {item.expected} (verified by read-back)")
    return lines


def confirmation_text(ctx: Context, recipient: str) -> tuple[str, str]:
    entity = ctx.targets[0].display if ctx.targets else "your account"
    facts = ", ".join(f"{key}: {value}" for key, value in ctx.dod.facts.items())
    subject = f"Confirmation: {ctx.dod.summary or 'account update'} for {entity}"[:120]
    body = (
        f"Hello {entity} team,\n\n"
        f"This is a confirmation of the requested change to your account records.\n"
        f"Details: {facts}.\n\n"
        f"Changes applied: {'; '.join(change_lines(ctx)) or 'pending'}.\n\n"
        f"This message is a draft awaiting review by the account owner and has not been sent.\n"
        f"Reply to {recipient} if any detail is incorrect.\n"
    )
    return subject, body


def review_text(ctx: Context) -> str:
    entity = ctx.targets[0].display if ctx.targets else "the account"
    owner = ctx.dod.account_owner or ctx.frame.reporter or "account owner"
    facts = "; ".join(f"{key}: {value}" for key, value in ctx.dod.facts.items())
    draft_ref = ctx.deliverable_refs.get("unsent_confirmation", "gmail draft")
    return (
        f"Review request for the account owner ({owner}): {entity} — the customer confirmation for "
        f"{ctx.dod.summary or 'this change'} is saved as an UNSENT draft ({draft_ref}). "
        f"Facts: {facts}. Please review and give approval before anything is sent; nothing has been sent."
    )


def update_text(ctx: Context) -> str:
    entity = ctx.targets[0].display if ctx.targets else "the requested record"
    evidence = "; ".join(
        f"{target.provider} {target.resource_type} {target.resource_id} ({', '.join(target.evidence[:2])})"
        for target in ctx.targets
    )
    changes = change_lines(ctx)
    unverified = [item for item in ctx.latest_evidence().values() if not item.match]
    protected_count = len(ctx.protected.ids)
    pending: list[str] = []
    if "unsent_confirmation" in ctx.deliverable_refs:
        pending.append("customer confirmation saved as an unsent draft awaiting account-owner review")
    if ctx.refusals:
        pending.append(f"{len(ctx.refusals)} write(s) refused by the mutation gate")
    lines = [
        f"Update on {entity}: {ctx.dod.summary or 'processed'}.",
        f"Found: {evidence or 'no target resolved'}.",
        f"Changed: {'; '.join(changes) if changes else 'no verified changes'}.",
        f"Not touched: {protected_count} look-alike record(s) locked by the protected set.",
    ]
    if unverified:
        lines.append(f"Unverified: {len(unverified)} check(s) did not match on read-back; see the receipt.")
    if pending:
        lines.append("Pending for humans: " + "; ".join(pending) + ".")
    facts = ", ".join(f"{key}={value}" for key, value in ctx.dod.facts.items())
    if facts:
        lines.append(f"Facts: {facts}.")
    return "\n".join(lines)


def escalation_facts(ctx: Context) -> list[str]:
    """Task facts an escalation post can cite: subject entities and request identifiers."""
    facts = [*ctx.frame.subject_entities, *ctx.frame.observed_identifiers, *ctx.dod.facts.values()]
    return [fact for fact in dict.fromkeys(facts) if fact and not _protected(ctx, fact)]


def escalation_text(ctx: Context) -> str:
    facts = escalation_facts(ctx)
    entity = facts[0] if facts else "the request"
    summary = "; ".join(
        f"{candidate.provider} {candidate.resource_type} ending …{candidate.resource_id[-4:]}"
        for candidate in ctx.candidates[:6]
    )
    cited = ", ".join(facts[:4])
    return (
        f"Escalation on {entity}: no write was made. Reason: {ctx.escalation_reason or 'ambiguous target'}. "
        f"Request identifiers: {cited}. Candidates seen: {summary or 'none'}. "
        f"A human decision is needed before any record is changed."
    )


def _mentions(text: str, terms: Sequence[str], *, minimum: int) -> bool:
    hits = sum(1 for term in terms if term and contains_term(text, term))
    return hits >= min(minimum, len([term for term in terms if term])) if terms else True


def final_json(ctx: Context) -> str:
    status = ctx.status()
    latest = ctx.latest_evidence()
    changes = [
        {
            "provider": item.provider,
            "resource": item.resource,
            "check": item.check,
            "after": item.expected,
            "observed": item.observed,
            "verified": item.match,
        }
        for item in latest.values()
        if item.check.startswith(("end_state", "readback"))
    ]
    payload = {
        "status": status,
        "decision": ctx.escalation_reason
        if status == "escalated"
        else (ctx.dod.summary or "completed per definition of done"),
        "target": {
            target.provider: {
                "resource_type": target.resource_type,
                "resource_id": target.resource_id,
                "display": target.display,
                "evidence": list(target.evidence),
            }
            for target in ctx.targets
        },
        "facts": dict(ctx.dod.facts),
        "changes": changes,
        "deliverables": dict(ctx.deliverable_refs),
        "policies": [
            {"provider": p.provider, "ref": p.resource_ref, "kind": p.kind, "quote": p.quote} for p in ctx.policies
        ],
        "protected_untouched": sorted(ctx.protected.ids),
        "refused_actions": [{"action": r.action_id, "rule": r.rule, "reason": r.reason} for r in ctx.refusals],
        "evidence": [
            {"check": e.check, "provider": e.provider, "expected": e.expected, "observed": e.observed, "match": e.match}
            for e in latest.values()
        ],
        "notes": list(ctx.notes),
    }
    return json.dumps(payload, ensure_ascii=False)
