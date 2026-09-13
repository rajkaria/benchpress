"""P3 — Definition of done as data: model drafts it, code applies the always-on rules."""

from __future__ import annotations

import re
from typing import get_args

from benchpress.context import Context, DefinitionOfDone, Deliverable, EndStateItem, ForbiddenClass
from benchpress.phases.common import PhaseDeps, dump, safe_emit
from benchpress.phases.schemas import DoDDraft
from benchpress.prompts import render

FORBIDDEN_CLASSES: frozenset[str] = frozenset(get_args(ForbiddenClass))

_PROHIBITION_CLASSES: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (re.compile(r"send|e-?mail|external mail|message the customer|reply to the customer", re.I), ("send_email",)),
    (re.compile(r"charge|refund|payment|invoice", re.I), ("create_charge", "create_invoice")),
    (re.compile(r"subscription", re.I), ("update_subscription", "move_subscription")),
    (re.compile(r"merge|push|rewrite|edit source|commit", re.I), ("merge_pr", "push_commit", "edit_source")),
    (re.compile(r"disable|rerun|re-run", re.I), ("disable_workflow", "mass_rerun")),
    (re.compile(r"close (?:the )?(?:regression|issue|ticket)", re.I), ("close_regression",)),
    (
        re.compile(r"publish|post publicly|share externally|external(?:ly)? shar", re.I),
        ("publish_external", "external_share"),
    ),
    (re.compile(r"invite|attendee", re.I), ("calendar_invite_attendees",)),
)
_NEW_VALUE_KEY = re.compile(r"verified|new|updated|target|correct|approved", re.I)


async def define_done(deps: PhaseDeps) -> None:
    ctx = deps.ctx
    deps.bus.enter("P3")
    draft = await safe_emit(
        deps,
        "dod",
        DoDDraft,
        render(
            "dod",
            frame=ctx.frame,
            policies=[dump(policy) for policy in ctx.policies],
            targets=[dump(target) for target in ctx.targets],
            protected=list(ctx.protected.all_terms()),
        ),
        DoDDraft(summary="", escalate=True, escalation_reason="model unavailable"),
    )
    ctx.dod = apply_code_rules(ctx, draft)
    if draft.escalate and not ctx.ambiguous:
        ctx.ambiguous = True
        ctx.escalation_reason = draft.escalation_reason or "the definition of done requires escalation"


def apply_code_rules(ctx: Context, draft: DoDDraft) -> DefinitionOfDone:
    """Generic rules that hold for every task, regardless of what the model drafted."""
    target_refs = {(target.provider, target.ref) for target in ctx.targets}
    end_state = tuple(
        EndStateItem(
            provider=item.provider,
            resource=item.resource,
            field=item.field,
            expected=item.expected,
            comparison=item.comparison,
        )
        for item in draft.end_state
        if (item.provider, item.resource) in target_refs and item.field and item.expected
    )
    facts = {key: value for key, value in draft.facts.items() if value}
    for index, item in enumerate(end_state):
        if item.expected not in facts.values():
            facts[f"expected_{item.provider}_{item.field}_{index}"] = item.expected

    entity = (
        ctx.targets[0].display if ctx.targets else (ctx.frame.subject_entities[0] if ctx.frame.subject_entities else "")
    )
    new_values = [value for key, value in facts.items() if _NEW_VALUE_KEY.search(key)] or list(facts.values())

    review_policies = ctx.policies_of("communication_review")
    needs_review = bool(review_policies) or draft.needs_customer_confirmation or draft.needs_owner_review
    deliverables: list[Deliverable] = [
        Deliverable(
            kind="originating_channel_update",
            provider="slack" if "slack" in ctx.providers else None,
            channel=ctx.frame.originating_channel,
            must_mention=tuple(value for value in (entity, *new_values[:2]) if value),
        )
    ]
    if needs_review:
        because = review_policies[0].citation if review_policies else "model:needs_customer_confirmation"
        deliverables.append(
            Deliverable(
                kind="unsent_customer_confirmation",
                provider="gmail" if "gmail" in ctx.providers else None,
                because=because,
                must_mention=tuple(value for value in (entity, *list(facts.values())[:3]) if value),
            )
        )
        record_provider = next((name for name in ("slack", "jira", "linear") if name in ctx.providers), None)
        deliverables.append(
            Deliverable(
                kind="owner_review_record",
                provider=record_provider,
                channel=ctx.frame.originating_channel,
                because=because,
                must_mention=tuple(value for value in ("review", "owner", entity) if value),
            )
        )
    deliverables.append(Deliverable(kind="structured_result"))

    forbidden = {label for label in draft.forbidden if label in FORBIDDEN_CLASSES}
    forbidden |= {"delete_any", "mutate_protected"}
    for phrase in ctx.frame.explicit_prohibitions:
        for pattern, classes in _PROHIBITION_CLASSES:
            if pattern.search(phrase):
                forbidden.update(classes)
    if needs_review:
        forbidden.add("send_email")

    write_scope = {item.provider for item in end_state} | {item.provider for item in deliverables if item.provider}
    return DefinitionOfDone(
        end_state=end_state,
        deliverables=tuple(deliverables),
        forbidden=tuple(sorted(forbidden)),
        write_scope=tuple(sorted(write_scope & set(ctx.providers))),
        facts=facts,
        escalation=draft.escalation_reason if draft.escalate else None,
        summary=draft.summary,
        customer_contact=draft.customer_contact_email,
        account_owner=draft.account_owner,
    )
