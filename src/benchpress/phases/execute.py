"""P5 — Execute through the mutation gate, then read every write back."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import cast

from benchpress.context import Action, Evidence
from benchpress.normalize import canonical_email, contains_term
from benchpress.phases.common import PhaseDeps
from benchpress.playbooks import extract_field
from benchpress.tools import BudgetExhausted

_WRAPPERS = frozenset({"properties", "message", "fields", "data", "input"})


async def execute(deps: PhaseDeps, actions: Sequence[Action] | None = None, *, phase: str = "P5") -> None:
    ctx = deps.ctx
    deps.bus.enter(phase)
    for action in ctx.plan.actions if actions is None else actions:
        if not action.is_write:
            continue
        try:
            result, verdict = await deps.bus.perform(action)
        except BudgetExhausted:
            deps.note(f"{phase}: budget exhausted before {action.id}")
            return
        if not verdict.allowed:
            continue
        body = result.json()
        created = body.get("id") or body.get("ts")
        if result.ok and isinstance(created, (str, int)):
            ctx.created[action.id] = str(created)
        if not result.ok:
            ctx.add_evidence(
                [
                    Evidence(
                        check=f"write:{action.id}",
                        provider=action.provider,
                        resource=resource_of(action),
                        expected="2xx",
                        observed=f"{result.status_code}: {str(result.error or '')[:200]}",
                        match=False,
                    )
                ]
            )
            continue
        if action.readback is not None and not deps.ablations.no_readback:
            await readback(deps, action, body)


async def readback(deps: PhaseDeps, action: Action, response_body: Mapping[str, object]) -> None:
    ctx = deps.ctx
    spec = action.readback
    if spec is None:
        return
    path = fill_placeholders(spec.path, response_body)
    query = {key: fill_placeholders(value, response_body) for key, value in spec.query.items()}
    try:
        result = await deps.bus.read(action.provider, path, query=query, method=spec.method, body=spec.body)
    except BudgetExhausted:
        deps.note(f"readback skipped for {action.id}: budget exhausted")
        return
    if not result.ok:
        ctx.add_evidence(
            [
                Evidence(
                    check=f"readback:{action.id}",
                    provider=action.provider,
                    resource=resource_of(action),
                    expected="readable",
                    observed=f"{result.status_code}: {str(result.error or '')[:120]}",
                    match=False,
                )
            ]
        )
        return
    for field_name in action.fields:
        if field_name in _WRAPPERS:
            continue
        expected = leaf_value(action.body, field_name)
        if expected is None:
            continue
        observed = observe(result.body, field_name, spec.field_path)
        if observed is None:
            continue
        ctx.add_evidence(
            [
                Evidence(
                    check=f"readback:{action.id}:{field_name}",
                    provider=action.provider,
                    resource=resource_of(action),
                    expected=expected,
                    observed=observed,
                    match=values_match(expected, observed),
                )
            ]
        )


def observe(payload: object, field_name: str, field_path: str | None) -> str | None:
    candidates: list[str] = []
    if field_path:
        if field_path.endswith(field_name):
            candidates.append(field_path)
        candidates.append(f"{field_path}.{field_name}")
    candidates.extend([field_name, f"properties.{field_name}"])
    for candidate in candidates:
        value = extract_field(payload, candidate)
        if value is not None:
            return value
    return None


def values_match(expected: str, observed: str) -> bool:
    if "@" in expected and " " not in expected.strip():
        return canonical_email(expected) == canonical_email(observed)
    return contains_term(observed, expected) or contains_term(expected, observed)


def fill_placeholders(template: str, response_body: Mapping[str, object]) -> str:
    created_id = response_body.get("id")
    created_ts = response_body.get("ts")
    filled = template
    if isinstance(created_id, (str, int)):
        filled = filled.replace("{created_id}", str(created_id))
    if isinstance(created_ts, (str, int, float)):
        filled = filled.replace("{created_ts}", str(created_ts))
    return filled


def leaf_value(body: object, field_name: str) -> str | None:
    if isinstance(body, Mapping):
        typed = cast(Mapping[str, object], body)
        if field_name in typed and isinstance(typed[field_name], (str, int, float, bool)):
            return str(typed[field_name])
        for value in typed.values():
            found = leaf_value(value, field_name)
            if found is not None:
                return found
    return None


def resource_of(action: Action) -> str:
    return action.target_refs[0] if action.target_refs else action.path
