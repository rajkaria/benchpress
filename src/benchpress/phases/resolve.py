"""P2 — Enumerate every plausible target, resolve with cited evidence, lock the rest."""

from __future__ import annotations

from benchpress.context import Candidate, ResolvedTarget
from benchpress.normalize import LOOKALIKE_QUALIFIERS, contains_term, tokens
from benchpress.phases.common import PhaseDeps, dump, safe_emit
from benchpress.phases.schemas import Resolution
from benchpress.prompts import render
from benchpress.tools import BudgetExhausted

_NO_TARGET_PROVIDERS = frozenset({"slack", "gmail"})
_SNAPSHOT_CAP = 4


async def resolve(deps: PhaseDeps) -> None:
    ctx = deps.ctx
    deps.bus.enter("P2")
    entities = list(ctx.frame.subject_entities)
    hints = list(ctx.frame.observed_identifiers)
    if not entities:
        entities = [hint for hint in hints if "@" not in hint][:2]
    candidates: list[Candidate] = []
    for provider in ctx.providers:
        playbook = deps.playbook(provider)
        if playbook is None:
            continue
        try:
            for entity in entities:
                candidates.extend(await playbook.find_candidates(deps.bus, entity, hints))
        except BudgetExhausted:
            deps.note(f"P2: budget exhausted while enumerating {provider}")
            break
    ctx.candidates = _dedupe(candidates)
    if not ctx.candidates:
        ctx.ambiguous = True
        ctx.escalation_reason = "no candidate records were found for the subject of the request"
        return

    resolution = await safe_emit(
        deps,
        "resolve",
        Resolution,
        render(
            "resolve",
            frame=ctx.frame,
            policies=[dump(policy) for policy in ctx.policies],
            candidates=[dump(candidate) for candidate in ctx.candidates],
        ),
        Resolution(ambiguous=True, ambiguity_reason="model unavailable"),
    )
    by_ref = {(candidate.provider, candidate.ref): candidate for candidate in ctx.candidates}
    chosen: list[ResolvedTarget] = []
    for choice in resolution.chosen:
        candidate = by_ref.get((choice.provider, f"{choice.resource_type}:{choice.resource_id}"))
        if candidate is None:
            deps.note(
                f"P2: model chose an unknown record {choice.provider}:{choice.resource_type}:{choice.resource_id}"
            )
            continue
        haystack = candidate_text(candidate)
        evidence = tuple(item for item in choice.evidence if item.strip() and contains_term(haystack, item))
        if not evidence:
            deps.note(f"P2: evidence for {candidate.display!r} could not be verified against the record")
            continue
        if is_qualified_variant(candidate.display, entities):
            deps.note(f"P2: refused to choose qualified variant {candidate.display!r}")
            continue
        if any(t.provider == candidate.provider and t.resource_type == candidate.resource_type for t in chosen):
            continue
        chosen.append(
            ResolvedTarget(
                provider=candidate.provider,
                resource_type=candidate.resource_type,
                resource_id=candidate.resource_id,
                display=candidate.display,
                evidence=evidence,
                confidence=choice.confidence,
            )
        )
    ctx.targets = chosen

    chosen_keys = {(target.provider, target.ref) for target in chosen}
    for candidate in ctx.candidates:
        if (candidate.provider, candidate.ref) not in chosen_keys:
            ctx.protected.add_candidate(candidate)
    for target in chosen:
        for duplicate in ctx.near_duplicates(target.display):
            if (duplicate.provider, duplicate.ref) not in chosen_keys:
                ctx.protected.add_candidate(duplicate)
    for target in chosen:
        candidate = by_ref[(target.provider, target.ref)]
        allowed = [value for value in (candidate.name, candidate.domain, candidate.email) if value]
        ctx.protected.discard_target(target, allowed)

    providers_with_candidates = {candidate.provider for candidate in ctx.candidates}
    unresolved = sorted(
        provider
        for provider in providers_with_candidates
        if provider not in {target.provider for target in chosen} and provider not in _NO_TARGET_PROVIDERS
    )
    if resolution.ambiguous or unresolved or not chosen:
        ctx.ambiguous = True
        ctx.escalation_reason = resolution.ambiguity_reason or (
            f"no single target could be resolved in {', '.join(unresolved) or 'any provider'}"
        )
        return

    await snapshot_protected(deps)


async def snapshot_protected(deps: PhaseDeps) -> None:
    """Remember the exact fields of the look-alikes so P6 can prove they never changed."""
    ctx = deps.ctx
    taken = 0
    for candidate in ctx.candidates:
        if taken >= _SNAPSHOT_CAP or candidate.resource_id not in ctx.protected.ids:
            continue
        playbook = deps.playbook(candidate.provider)
        if playbook is None:
            continue
        try:
            record = await playbook.read_record(deps.bus, candidate.ref)
        except BudgetExhausted:
            break
        if record is not None:
            ctx.candidate_snapshots[f"{candidate.provider}:{candidate.ref}"] = dict(record.fields)
            taken += 1


def candidate_text(candidate: Candidate) -> str:
    parts = [candidate.display, candidate.resource_id, candidate.resource_type, candidate.notes]
    parts.extend(value for value in (candidate.name, candidate.domain, candidate.email, candidate.lifecycle) if value)
    return " ".join(parts)


def is_qualified_variant(display: str, entities: list[str]) -> bool:
    """`<Entity> Prospect` / `— Archive` / `Test` variants are never the target unless the
    request itself names the qualifier."""
    qualifiers = tokens(display) & LOOKALIKE_QUALIFIERS
    if not qualifiers:
        return False
    named = set[str]()
    for entity in entities:
        named |= tokens(entity)
    return not (qualifiers <= named)


def _dedupe(candidates: list[Candidate]) -> list[Candidate]:
    seen: set[tuple[str, str]] = set()
    unique: list[Candidate] = []
    for candidate in candidates:
        key = (candidate.provider, candidate.ref)
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique
