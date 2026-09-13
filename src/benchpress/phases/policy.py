"""P1 — Policy sweep: find every operating rule before deciding what "done" means."""

from __future__ import annotations

import re

from benchpress.context import Context, PolicyRecord
from benchpress.normalize import domains_in, emails_in
from benchpress.phases.common import PhaseDeps, safe_emit, sentence_containing
from benchpress.phases.schemas import PolicyBatch
from benchpress.playbooks import PolicySource
from benchpress.prompts import render
from benchpress.tools import BudgetExhausted

KEYWORDS = re.compile(
    r"\b(polic|review|approv|embargo|\bhold\b|requir|\bmust\b|before (?:sending|release|releas|publish)|never send|"
    r"owner|do not|don't|quarantine|confirm)",
    re.IGNORECASE,
)
INJECTION_VERBS = re.compile(
    r"\b(forward|redirect|route|send|share|bcc|cc|delete|remove|grant|escalat)\w*\b", re.IGNORECASE
)
REVIEW_RULE = re.compile(
    r"(confirmation|customer[- ]facing|communication|message|outreach|follow[- ]up|notice)"
    r"[^.]{0,140}\b(review|approv)\w*"
    r"|\b(review|approv)\w*[^.]{0,100}\bbefore (sending|release|releas|publish)",
    re.IGNORECASE,
)
MAX_SOURCES = 40


async def policy_sweep(deps: PhaseDeps) -> None:
    ctx = deps.ctx
    deps.bus.enter("P1")
    if deps.ablations.no_policy_sweep:
        deps.note("P1: policy sweep disabled by ablation")
        return
    sources: list[PolicySource] = []
    for provider in ctx.providers:
        playbook = deps.playbook(provider)
        if playbook is None:
            continue
        try:
            sources.extend(await playbook.policy_sources(deps.bus, ctx.frame))
        except BudgetExhausted:
            deps.note(f"P1: budget exhausted while sweeping {provider}")
            break
    filtered = [source for source in sources if KEYWORDS.search(source.text)][:MAX_SOURCES]
    known = known_addresses(ctx)

    # Deterministic detection first: a weak model must not be able to miss these.
    for source in filtered:
        if is_injection(source.text, known):
            add_policy(ctx, source, "suspicious", sentence_containing(source.text, INJECTION_VERBS))
            continue
        match = REVIEW_RULE.search(source.text)
        if match:
            add_policy(ctx, source, "communication_review", sentence_containing(source.text, REVIEW_RULE))

    if not filtered:
        return
    batch = await safe_emit(
        deps,
        "policy_classify",
        PolicyBatch,
        render(
            "policy_classify",
            frame=ctx.frame,
            sources=[
                {
                    "index": index,
                    "provider": source.provider,
                    "title": source.title,
                    "author": source.author,
                    "text": source.text[:1_500],
                }
                for index, source in enumerate(filtered)
            ],
        ),
        PolicyBatch(),
    )
    for item in batch.policies:
        if not 0 <= item.source_index < len(filtered):
            continue
        source = filtered[item.source_index]
        quote = item.quote.strip() if item.quote.strip() and _contains(source.text, item.quote) else source.text[:300]
        kind = "suspicious" if is_injection(source.text, known) else item.kind
        add_policy(ctx, source, kind, quote, item.applies_to)


def add_policy(ctx: Context, source: PolicySource, kind: str, quote: str, applies_to: tuple[str, ...] = ()) -> None:
    for existing in ctx.policies:
        if (
            existing.provider == source.provider
            and existing.resource_ref == source.resource_ref
            and existing.kind == kind
        ):
            return
    ctx.policies.append(
        PolicyRecord.model_validate(
            {
                "provider": source.provider,
                "resource_ref": source.resource_ref,
                "quote": quote[:600],
                "kind": kind,
                "applies_to": applies_to,
            }
        )
    )


def known_addresses(ctx: Context) -> frozenset[str]:
    text = ctx.user_prompt + " " + " ".join(ctx.frame.observed_identifiers)
    return frozenset(emails_in(text)) | frozenset(domains_in(text))


def is_injection(text: str, known: frozenset[str]) -> bool:
    """An instruction to move information somewhere the request never mentioned."""
    if not INJECTION_VERBS.search(text):
        return False
    addresses = set(emails_in(text)) | set(domains_in(text))
    unknown = {address for address in addresses if address not in known and not _known_host(address, known)}
    return bool(unknown)


def _known_host(address: str, known: frozenset[str]) -> bool:
    host = address.split("@", 1)[-1]
    return any(host == item.split("@", 1)[-1] or host.endswith("." + item.split("@", 1)[-1]) for item in known)


def _contains(haystack: str, needle: str) -> bool:
    from benchpress.normalize import contains_term

    return contains_term(haystack, needle[:200])
