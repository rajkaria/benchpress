"""P5 — Execute through the mutation gate, then read every write back."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from benchpress.context import Action, Evidence
from benchpress.normalize import canonical_email, contains_term
from benchpress.phases.common import PhaseDeps
from benchpress.playbooks import extract_field
from benchpress.tools import BudgetExhausted, ToolBus, ToolResult

_WRAPPERS = frozenset({"properties", "message", "fields", "data", "input"})
_BRACKET_KEY = re.compile(r"\[([^\]]*)\]")


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
    if action.readback is None:
        return
    result = await perform_readback(deps.bus, action, response_body)
    if result is None:
        deps.note(f"readback skipped for {action.id}: budget exhausted")
        return
    ctx.add_evidence(readback_evidence(action, result))


async def perform_readback(bus: ToolBus, action: Action, response_body: Mapping[str, object]) -> ToolResult | None:
    """Send the read-back request `action.readback` declares, placeholders filled from the write's response.

    Returns `None` when there is nothing to read (no read-back declared) or the bus has no budget left for it;
    never raises `BudgetExhausted`. Shared by `readback()` (the run-loop's P5 phase) and `VerifiedWrite`.
    """
    spec = action.readback
    if spec is None:
        return None
    path = fill_placeholders(spec.path, response_body)
    query = {key: fill_placeholders(value, response_body) for key, value in spec.query.items()}
    try:
        return await bus.read(action.provider, path, query=query, method=spec.method, body=spec.body)
    except BudgetExhausted:
        return None


def is_unreadable(action: Action, item: Evidence) -> bool:
    """True for the evidence `readback_evidence` emits when the read-back itself failed (nothing was observed)."""
    return item.check == f"readback:{action.id}" and not item.match


def readback_evidence(action: Action, read: ToolResult) -> list[Evidence]:
    """The read-back evidence for one write, given the already-performed read.

    Pure: no I/O, no context mutation. Shared by `readback()` (the run-loop's P5 phase) and
    `VerifiedWrite` (the model-free, controller-free primitive) so the field-matching logic
    that decides `verified` vs `mismatch` lives in exactly one place. A declared field the
    successful read-back does not show is non-matching evidence with `observed="missing"`.
    """
    resource = resource_of(action)
    if not read.ok:
        return [
            Evidence(
                check=f"readback:{action.id}",
                provider=action.provider,
                resource=resource,
                expected="readable",
                observed=f"{read.status_code}: {str(read.error or '')[:120]}",
                match=False,
            )
        ]
    return [
        Evidence(
            check=f"readback:{action.id}:{check.field}",
            provider=action.provider,
            resource=resource,
            expected=check.expected,
            observed=MISSING if check.observed is None else check.observed,
            match=check.match,
            detail="" if check.observed is not None else f"{check.field} not found at {', '.join(check.tried)}",
        )
        for check in field_checks(action, read.body)
    ]


MISSING = "missing"


@dataclass(frozen=True)
class FieldCheck:
    """One written field compared against a read-back body. `observed is None` means the body does not show it."""

    field: str
    expected: str
    observed: str | None
    tried: tuple[str, ...]

    @property
    def match(self) -> bool:
        return self.observed is not None and values_match(self.expected, self.observed)


def field_checks(action: Action, read_body: object) -> list[FieldCheck]:
    """Every field the read-back must show: `action.fields` minus wrapper keys, non-scalar values and `unobserved`."""
    spec = action.readback
    field_path = spec.field_path if spec else None
    unobserved = frozenset(spec.unobserved) if spec else frozenset[str]()
    checks: list[FieldCheck] = []
    for field_name in action.fields:
        if field_name in _WRAPPERS or field_name in unobserved:
            continue
        expected = leaf_value(action.body, field_name)
        if expected is None:
            continue
        tried = candidate_paths(field_name, field_path)
        checks.append(FieldCheck(field_name, expected, first_value(read_body, tried), tried))
    return checks


def observe(payload: object, field_name: str, field_path: str | None) -> str | None:
    return first_value(payload, candidate_paths(field_name, field_path))


def candidate_paths(field_name: str, field_path: str | None) -> tuple[str, ...]:
    """Where a written field may sit in a read-back body, most specific first.

    Form-style names are read as dotted paths (`metadata[lifecycle]` -> `metadata.lifecycle`). With a `field_path`,
    the field is looked for at the path itself, beneath it, and beside it (`messages.0.text` -> `messages.0.thread_ts`).
    """
    dotted = _dotted(field_name)
    candidates: list[str] = []
    if field_path:
        if field_path == dotted or field_path.endswith(f".{dotted}"):
            candidates.append(field_path)
        candidates.append(f"{field_path}.{dotted}")
        parent = field_path.rpartition(".")[0]
        if parent:
            candidates.append(f"{parent}.{dotted}")
    candidates.extend([dotted, f"properties.{dotted}"])
    return tuple(dict.fromkeys(candidates))


def first_value(payload: object, paths: Sequence[str]) -> str | None:
    for path in paths:
        value = extract_field(payload, path)
        if value is not None:
            return value
    return None


def _dotted(field_name: str) -> str:
    head, _, rest = field_name.partition("[")
    if not rest:
        return field_name
    return ".".join([head, *_BRACKET_KEY.findall(f"[{rest}")])


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
