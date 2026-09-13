"""`benchpress regress`: every gate decision a run made becomes a permanent gate-corpus case.

A receipt records each write the gate judged (`gate_decisions`: the exact action and verdict) and the
facts the gate consulted (targets, candidates, protected set, definition of done, policies, packs).
`regress` turns each decision that matters (every refused write with its rule, every allowed write)
into a `GateCase`, redacts message bodies down to the tokens the decision depends on, and proves the
case replays on the current gate before writing it. `benchpress gate check DIR` then replays them.

Redaction is verified, never assumed: a string is reduced to the emails, destination domains and
protected terms it carries, the case is re-run, and only when the reduced case no longer reproduces
the recorded decision does the case fall back to the recorded body (and say so in its tags).

Receipts written before `gate_decisions` existed are still usable: allowed writes are rebuilt from the
ledger, the plan and `benchpress-trace.jsonl` (the request body at the same ledger sequence), and
refusals from the plan when the refused action is still on it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import yaml
from pydantic import ValidationError

from benchpress.context import Action, GateVerdict, ProtectedSet
from benchpress.gate import decode_base64url_text, fingerprint
from benchpress.gate_corpus import (
    BASE64URL_KEY,
    GATE_RULES,
    CaseAction,
    CaseContext,
    CaseExpect,
    CaseProtected,
    CorpusError,
    GateCase,
    run_case,
)
from benchpress.normalize import destination_domains_in, domains_in, emails_in
from benchpress.packs import PACK_RULE_PREFIX, PolicyPackError

REDACTED = "redacted"
TRACE_FILE = "benchpress-trace.jsonl"


class RegressError(ValueError):
    """A receipt that cannot be read or that carries no gate decisions to regress."""


@dataclass(frozen=True)
class Decision:
    """One write the gate judged, as recovered from a receipt."""

    phase: str
    action: Action
    verdict: GateVerdict
    origin: str  # "gate_decisions" | "ledger+trace" | "plan"


@dataclass(frozen=True)
class RegressedCase:
    case: GateCase
    redacted: bool


@dataclass(frozen=True)
class Drift:
    """A recorded decision the current gate no longer makes (the gate changed, or the context is incomplete)."""

    decision: Decision
    actual: str


@dataclass(frozen=True)
class RegressResult:
    receipt: Path
    cases: tuple[RegressedCase, ...]
    drift: tuple[Drift, ...]
    skipped: tuple[str, ...]

    def summary(self, out: Path | None = None) -> str:
        lines = [
            f"regress {self.receipt}: {len(self.cases)} case(s), {len(self.drift)} drift, {len(self.skipped)} skipped"
        ]
        for item in self.cases:
            mark = "redacted" if item.redacted else "recorded body"
            lines.append(f"  {item.case.expect.label:<28} {item.case.name}  ({mark})")
        for drift in self.drift:
            verdict = drift.decision.verdict
            recorded = "allow" if verdict.allowed else f"refuse:{verdict.rule}"
            lines.append(f"  DRIFT {drift.decision.action.id}: recorded {recorded}, current gate {drift.actual}")
        lines.extend(f"  skipped: {note}" for note in self.skipped)
        if out is not None:
            lines.append(f"\nwrote {out}\nreplay: benchpress gate check {out.parent}")
        return "\n".join(lines)


# --------------------------------------------------------------------------------------
# Reading a receipt
# --------------------------------------------------------------------------------------


def resolve_receipt(path: Path) -> Path:
    return path / "receipt.json" if path.is_dir() else path


def load_receipt(path: Path) -> dict[str, Any]:
    receipt = resolve_receipt(path)
    try:
        raw: object = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RegressError(f"{receipt}: cannot read receipt: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise RegressError(f"{receipt}: a receipt is a JSON object")
    return dict(cast(Mapping[str, Any], raw))


def records(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(cast(Mapping[str, Any], item)) for item in cast(list[object], value) if isinstance(item, Mapping)]


def mapping(value: object) -> dict[str, Any]:
    return dict(cast(Mapping[str, Any], value)) if isinstance(value, Mapping) else {}


def strings(value: object) -> tuple[str, ...]:
    return tuple(str(item) for item in cast(list[object], value)) if isinstance(value, list) else ()


def read_trace(receipt: Path) -> dict[int, dict[str, Any]]:
    """Trace `api` events by ledger sequence (the request as it left the process, secrets already redacted)."""
    trace = resolve_receipt(receipt).parent / TRACE_FILE
    events: dict[int, dict[str, Any]] = {}
    if not trace.is_file():
        return events
    for line in trace.read_text(encoding="utf-8").splitlines():
        try:
            event: object = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, Mapping):
            item = dict(cast(Mapping[str, Any], event))
            if item.get("kind") == "api" and isinstance(item.get("sequence"), int):
                events[cast(int, item["sequence"])] = item
    return events


def decisions_from_receipt(payload: Mapping[str, Any], receipt: Path) -> tuple[list[Decision], list[str]]:
    """Every gate decision in the receipt, deduplicated by (action fingerprint, verdict)."""
    skipped: list[str] = []
    found: list[Decision] = []
    recorded = records(payload.get("gate_decisions"))
    if recorded:
        for item in recorded:
            try:
                action = Action.model_validate(item.get("action"))
                verdict = GateVerdict.model_validate(item.get("verdict"))
            except ValidationError as exc:
                skipped.append(f"unreadable gate decision: {exc.errors()[0].get('msg')}")
                continue
            found.append(Decision(str(item.get("phase") or ""), action, verdict, "gate_decisions"))
    else:
        found, skipped = _legacy_decisions(payload, receipt)
    unique: list[Decision] = []
    seen: set[tuple[str, bool, str]] = set()
    for decision in found:
        if decision.verdict.rule == "ablated":
            skipped.append(f"{decision.action.id}: judged under the no_gate ablation (nothing was enforced)")
            continue
        key = (fingerprint(decision.action), decision.verdict.allowed, decision.verdict.rule)
        if key in seen:
            continue
        seen.add(key)
        unique.append(decision)
    return unique, skipped


def _legacy_decisions(payload: Mapping[str, Any], receipt: Path) -> tuple[list[Decision], list[str]]:
    plan: dict[str, Action] = {}
    for item in records(payload.get("plan")):
        try:
            action = Action.model_validate(item)
        except ValidationError:
            continue
        plan[action.id] = action
    trace = read_trace(receipt)
    found: list[Decision] = []
    skipped: list[str] = []
    for entry in records(payload.get("ledger")):
        gate = mapping(entry.get("gate"))
        action_id = str(entry.get("action_id") or "")
        if not gate or not action_id or not gate.get("allowed"):
            continue
        planned = plan.get(action_id)
        event = mapping(trace.get(int(entry.get("sequence") or 0), {}).get("request"))
        if planned is None and not event:
            skipped.append(f"{action_id}: allowed write with neither a plan entry nor a trace request")
            continue
        base: dict[str, Any] = planned.model_dump() if planned else {"id": action_id, "kind": "update"}
        base.update({"provider": entry.get("provider"), "method": entry.get("method"), "path": entry.get("path")})
        if event:
            base["query"] = mapping(event.get("query"))
            base["body"] = event.get("body")
        try:
            found.append(
                Decision(
                    str(entry.get("phase") or ""),
                    Action.model_validate(base),
                    GateVerdict.model_validate(gate),
                    "ledger+trace",
                )
            )
        except ValidationError:
            skipped.append(f"{action_id}: ledger entry does not rebuild an action")
    for item in records(payload.get("refusals")):
        verdict = GateVerdict.model_validate(item)
        planned = plan.get(verdict.action_id)
        if planned is None:
            skipped.append(
                f"{verdict.action_id}: refused [{verdict.rule}] but the receipt predates gate_decisions and the "
                "action left the plan, so its request cannot be rebuilt"
            )
            continue
        found.append(Decision("", planned, verdict, "plan"))
    return found, skipped


# --------------------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------------------


def _protected_terms(protected: CaseProtected) -> ProtectedSet:
    return ProtectedSet(
        ids=set(protected.ids),
        names=set(protected.names),
        domains={item.casefold() for item in protected.domains},
        emails={item.casefold() for item in protected.emails},
    )


def reduce_text(text: str, protected: ProtectedSet) -> str:
    """The tokens of `text` the gate can decide on: emails, destination domains, protected terms."""
    kept: list[str] = []
    for token in sorted(emails_in(text)) + sorted(destination_domains_in(text) - emails_in(text)):
        if token not in kept:
            kept.append(token)
    for term in protected.all_terms():
        single = ProtectedSet(
            ids={term} & protected.ids,
            names={term} & protected.names,
            domains={term} & protected.domains,
            emails={term} & protected.emails,
        )
        if single.hit(text) and term not in kept:
            kept.append(term)
    return " ".join([REDACTED, *kept])


def redact_value(value: object, protected: ProtectedSet) -> object:
    """Keep every key (field names drive field smuggling), reduce every string leaf.

    A base64url `raw` field is decoded, reduced, and written back as `{$base64url: text}` so the
    case stays reviewable and the gate still reads the decoded text.
    """
    if isinstance(value, Mapping):
        out: dict[str, object] = {}
        for raw_key, item in cast(Mapping[object, object], value).items():
            key = str(raw_key)
            if key == "raw" and isinstance(item, str):
                decoded = decode_base64url_text(item)
                if decoded:
                    out[key] = {BASE64URL_KEY: reduce_text(decoded, protected)}
                    continue
            out[key] = redact_value(item, protected)
        return out
    if isinstance(value, list):
        return [redact_value(item, protected) for item in cast(list[object], value)]
    if isinstance(value, str):
        return reduce_text(value, protected)
    return value


def unexpand_body(value: object) -> object:
    """The recorded body, with base64url `raw` fields written as plain text for review."""
    if isinstance(value, Mapping):
        out: dict[str, object] = {}
        for raw_key, item in cast(Mapping[object, object], value).items():
            key = str(raw_key)
            decoded = decode_base64url_text(item) if key == "raw" and isinstance(item, str) else ""
            reencoded = base64.urlsafe_b64encode(decoded.encode("utf-8")).decode("ascii").rstrip("=")
            out[key] = {BASE64URL_KEY: decoded} if decoded and reencoded == item else unexpand_body(item)
        return out
    if isinstance(value, list):
        return [unexpand_body(item) for item in cast(list[object], value)]
    return value


# --------------------------------------------------------------------------------------
# Building cases
# --------------------------------------------------------------------------------------


def slug(text: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", text.casefold())).strip("-") or "x"


def case_name(action: Action, verdict: GateVerdict, trial_id: str) -> str:
    """`<kind>-<provider>-<allowed|refused-rule>-<digest>`: never a record name, id, or address."""
    outcome = "allowed" if verdict.allowed else f"refused-{slug(verdict.rule)}"
    digest = hashlib.sha256(f"{trial_id}\n{fingerprint(action)}\n{outcome}".encode()).hexdigest()[:8]
    return f"{slug(action.kind)}-{slug(action.provider)}-{outcome}-{digest}"


def _quoted_term(reason: str) -> str | None:
    match = re.search(r"'([^']+)'", reason)
    return match.group(1) if match else None


def build_context(payload: Mapping[str, Any], *, redact: bool) -> CaseContext:
    request = mapping(payload.get("request"))
    dod = mapping(payload.get("definition_of_done"))
    protected_raw = mapping(payload.get("protected"))
    protected = CaseProtected(
        ids=strings(protected_raw.get("ids")),
        names=strings(protected_raw.get("names")),
        domains=strings(protected_raw.get("domains")),
        emails=strings(protected_raw.get("emails")),
    )
    prompt = str(request.get("prompt") or "")
    policies = records(payload.get("policies"))
    candidates = records(payload.get("candidates"))
    if redact:
        # Only domains in the prompt and policy quotes reach the gate (they mark a domain as internal).
        prompt = " ".join(sorted(domains_in(prompt)))
        policies = [
            {**policy, "quote": " ".join(sorted(domains_in(str(policy.get("quote") or ""))))} for policy in policies
        ]
        candidates = [{key: value for key, value in candidate.items() if key != "notes"} for candidate in candidates]
    return CaseContext.model_validate(
        {
            "prompt": prompt,
            "providers": strings(request.get("providers")),
            "forbidden": [item for item in strings(dod.get("forbidden"))],
            "write_scope": strings(dod.get("write_scope")),
            "facts": {str(key): str(value) for key, value in mapping(dod.get("facts")).items()},
            "protected": protected,
            "targets": records(payload.get("targets")),
            "candidates": candidates,
            "policies": policies,
        }
    )


def to_case_action(action: Action, body: object) -> CaseAction:
    return CaseAction(
        id=action.id,
        kind=action.kind,
        provider=action.provider,
        method=action.method,
        path=action.path,
        query=dict(action.query),
        body=body,
        fields=action.fields,
        target_refs=action.target_refs,
        satisfies=action.satisfies,
    )


def build_case(
    decision: Decision,
    payload: Mapping[str, Any],
    receipt: Path,
    *,
    redact: bool,
    plan: Mapping[str, Action],
) -> GateCase:
    action, verdict = decision.action, decision.verdict
    trial_id = str(payload.get("trial_id") or "")
    context = build_context(payload, redact=redact)
    protected = _protected_terms(context.protected)

    def body_of(item: Action) -> object:
        return redact_value(item.body, protected) if redact else unexpand_body(item.body)

    case_action = to_case_action(action, body_of(action))
    # The run's gate enforces plan membership. The judged action is on the plan unless the recorded
    # refusal is about plan membership itself, in which case the plan holds only what the receipt shows.
    if verdict.allowed or verdict.rule != "plan_membership":
        plan_actions: tuple[CaseAction, ...] = (case_action,)
    else:
        planned = plan.get(action.id)
        plan_actions = (to_case_action(planned, body_of(planned)),) if planned else ()
    succeeded = (case_action,) if verdict.rule == "idempotency" else ()
    context = context.model_copy(update={"plan": plan_actions, "enforce_plan": True, "succeeded": succeeded})
    if verdict.allowed:
        expect = CaseExpect(decision="allow")
    else:
        expect = CaseExpect(decision="refuse", rule=verdict.rule, reason_contains=_quoted_term(verdict.reason))
    packs = strings(payload.get("policy_packs"))
    source = f"{receipt}#{action.id}"
    phase = f" in {decision.phase}" if decision.phase else ""
    outcome = "allowed" if verdict.allowed else f"refused by rule {verdict.rule}"
    tags = ["regress", slug(action.provider), slug(action.kind), "allowed" if verdict.allowed else "refused"]
    if not verdict.allowed:
        tags.append(slug(verdict.rule))
    tags.append("redacted" if redact else "recorded-body")
    return GateCase(
        name=case_name(action, verdict, trial_id),
        description=(
            f"Regressed from receipt {receipt} (trial {trial_id}, action {action.id}{phase}): "
            f"the gate {outcome}. Origin: {decision.origin}."
        ),
        tags=tuple(tags),
        packs=packs,
        context=context,
        action=case_action,
        expect=expect,
        source=source,
    )


def _reproduces(case: GateCase) -> tuple[bool, str]:
    try:
        result = run_case(case)
    except (ValidationError, CorpusError, PolicyPackError) as exc:
        return False, f"error: {exc}"
    return result.matched, result.actual


def regress_receipt(path: Path) -> RegressResult:
    receipt = resolve_receipt(path)
    payload = load_receipt(receipt)
    decisions, skipped = decisions_from_receipt(payload, receipt)
    plan: dict[str, Action] = {}
    for item in records(payload.get("plan")):
        try:
            action = Action.model_validate(item)
        except ValidationError:
            continue
        plan[action.id] = action
    cases: list[RegressedCase] = []
    drift: list[Drift] = []
    for decision in decisions:
        rule = decision.verdict.rule
        if not decision.verdict.allowed and rule not in GATE_RULES and not rule.startswith(PACK_RULE_PREFIX):
            skipped.append(f"{decision.action.id}: unknown refusal rule {rule!r}")
            continue
        actual = ""
        for redact in (True, False):
            try:
                case = build_case(decision, payload, receipt, redact=redact, plan=plan)
            except (ValidationError, ValueError) as exc:
                actual = f"error: {exc}"
                continue
            matched, actual = _reproduces(case)
            if not matched and case.expect.reason_contains:
                # The reason quotes a term redaction may reshape; the rule is the contract.
                loose = case.model_copy(update={"expect": case.expect.model_copy(update={"reason_contains": None})})
                matched, actual = _reproduces(loose)
                case = loose if matched else case
            if matched:
                cases.append(RegressedCase(case=case, redacted=redact))
                break
        else:
            drift.append(Drift(decision=decision, actual=actual))
    return RegressResult(receipt=receipt, cases=tuple(cases), drift=tuple(drift), skipped=tuple(skipped))


# --------------------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------------------


class _CaseDumper(yaml.SafeDumper):
    def ignore_aliases(self, data: object) -> bool:
        return True


def _str_presenter(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    style = "|" if "\n" in data else None
    node: yaml.ScalarNode = cast(Any, dumper).represent_scalar("tag:yaml.org,2002:str", data, style=style)
    return node


_CaseDumper.add_representer(str, _str_presenter)


def case_document(case: GateCase) -> dict[str, Any]:
    """A case as corpus YAML: defaults dropped so a reviewer reads only what the decision rests on."""
    return case.model_dump(mode="json", exclude_defaults=True)


def write_cases(result: RegressResult, out_dir: Path, trial_id: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(str(result.receipt.resolve()).encode()).hexdigest()[:8]
    out = out_dir / f"regress-{slug(trial_id)}-{digest}.yaml"
    header = (
        f"# Regressed by `benchpress regress` from {result.receipt}.\n"
        "# One case per gate decision; bodies are reduced to the tokens the decision rests on.\n"
        f"# Replay: benchpress gate check {out_dir}\n"
    )
    document = {"cases": [case_document(item.case) for item in result.cases]}
    out.write_text(header + yaml.dump(document, Dumper=_CaseDumper, sort_keys=False, allow_unicode=True, width=120))
    return out


def regress_command(receipt: str, out_dir: str) -> int:
    """`benchpress regress`: 0 when every decision regressed, 1 on drift, 2 on an unusable receipt."""
    path = Path(receipt)
    try:
        result = regress_receipt(path)
    except RegressError as exc:
        print(f"regress: {exc}", file=sys.stderr)
        return 2
    if not result.cases and not result.drift:
        print(result.summary(), file=sys.stderr)
        print("regress: no gate decisions to regress", file=sys.stderr)
        return 2
    trial_id = str(load_receipt(path).get("trial_id") or "trial")
    out = write_cases(result, Path(out_dir), trial_id) if result.cases else None
    print(result.summary(out))
    return 1 if result.drift else 0
