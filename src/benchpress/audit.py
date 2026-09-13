"""`benchpress receipts export`: an audit log of every write attempt across one or many receipts.

One row per write the gate judged: when, which trial, who asked, which provider and record, which
field names changed (never the values), what the gate decided and under which rule, whether the
request went out, what the read-back showed, which definition-of-done items the write satisfied, which
discovered policies govern it, and the run's final status. Column order is a contract
(`AUDIT_COLUMNS`, documented in docs/AUDIT-EXPORT.md); new columns are only ever appended.
"""

from __future__ import annotations

import csv
import io
import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from benchpress.regress import load_receipt, mapping, records, strings

AUDIT_COLUMNS: tuple[str, ...] = (
    "timestamp",
    "trial_id",
    "receipt",
    "sequence",
    "phase",
    "requested_by",
    "agent_model",
    "provider",
    "method",
    "path",
    "action_id",
    "action_kind",
    "target_refs",
    "fields_changed",
    "gate_decision",
    "gate_rule",
    "gate_reason",
    "executed",
    "status_code",
    "provider_ok",
    "readback",
    "readback_checks",
    "dod_items",
    "dod_evidence",
    "policies",
    "final_status",
)
LIST_SEPARATOR = " | "
RECEIPT_PROTOCOL = "benchpress-receipt/1"

Row = dict[str, object]


class AuditError(ValueError):
    """A path that holds no readable Benchpress receipt."""


def find_receipts(paths: Iterable[str | Path]) -> tuple[Path, ...]:
    """Receipt files named directly, plus every `receipt.json` under a directory (sorted, deduplicated)."""
    found: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            found.extend(sorted(path.rglob("receipt.json")))
        elif path.is_file():
            found.append(path)
        else:
            raise AuditError(f"no such receipt file or directory: {path}")
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in found:
        key = path.resolve()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return tuple(unique)


# --------------------------------------------------------------------------------------
# Rows
# --------------------------------------------------------------------------------------


def _dod_item_label(item: str, dod: Mapping[str, Any]) -> str:
    """`end_state[0]: stripe customer:cus_1 email`: which record and field, never the expected value."""
    if item.startswith("end_state[") and item.endswith("]"):
        try:
            entry = records(dod.get("end_state"))[int(item[len("end_state[") : -1])]
        except (ValueError, IndexError):
            return item
        return f"{item}: {entry.get('provider')} {entry.get('resource')} {entry.get('field')}"
    return item


def _policies_for(
    satisfies: Sequence[str], target_refs: Sequence[str], dod: Mapping[str, Any], policies: list[dict[str, Any]]
) -> list[str]:
    """Policies a write answers to: cited by a deliverable it satisfies, or naming one of its targets."""
    citations: set[str] = set()
    for item in satisfies:
        if item.startswith("deliverable:"):
            kind = item.removeprefix("deliverable:")
            for deliverable in records(dod.get("deliverables")):
                if deliverable.get("kind") == kind and deliverable.get("because"):
                    citations.add(str(deliverable["because"]))
    refs = set(target_refs) | {ref.split(":", 1)[-1] for ref in target_refs}
    quoted: list[str] = []
    for policy in policies:
        citation = f"policy:{policy.get('provider')}:{policy.get('resource_ref')}"
        if citation in citations or refs & set(strings(policy.get("applies_to"))):
            quoted.append(f"[{policy.get('kind')}] {citation}: {policy.get('quote')}")
    return quoted


def _attempts(payload: Mapping[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
    """(attempt, ledger entry or None) per write attempt, in the order the gate judged them.

    An attempt is `{phase, at, action, verdict}`. Receipts with `gate_decisions` use them and pair each
    with its ledger entry (same action id and verdict, first unused). Older receipts fall back to the
    gated ledger entries plus refusals that never reached the ledger (dropped while planning).
    """
    ledger = [entry for entry in records(payload.get("ledger")) if entry.get("gate")]
    used: set[int] = set()
    plan = {str(item.get("id")): item for item in records(payload.get("plan"))}

    def pair(action_id: str, allowed: bool) -> dict[str, Any] | None:
        for index, entry in enumerate(ledger):
            gate = mapping(entry.get("gate"))
            if index not in used and entry.get("action_id") == action_id and bool(gate.get("allowed")) == allowed:
                used.add(index)
                return entry
        return None

    attempts: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
    decisions = records(payload.get("gate_decisions"))
    if decisions:
        for decision in decisions:
            action = mapping(decision.get("action"))
            verdict = mapping(decision.get("verdict"))
            entry = pair(str(action.get("id") or ""), bool(verdict.get("allowed")))
            attempts.append((decision, entry))
        return attempts
    for index, entry in enumerate(ledger):
        used.add(index)
        action_id = str(entry.get("action_id") or "")
        action = {**plan.get(action_id, {}), **{k: entry.get(k) for k in ("provider", "method", "path")}}
        action["id"] = action_id
        attempts.append(
            ({"phase": entry.get("phase"), "at": entry.get("at"), "action": action, "verdict": entry["gate"]}, entry)
        )
    ledgered = {(str(entry.get("action_id")), mapping(entry.get("gate")).get("rule")) for entry in ledger}
    for verdict in records(payload.get("refusals")):
        action_id = str(verdict.get("action_id") or "")
        if (action_id, verdict.get("rule")) in ledgered:
            continue
        attempts.append(
            ({"phase": "", "at": "", "action": {**plan.get(action_id, {}), "id": action_id}, "verdict": verdict}, None)
        )
    return attempts


def receipt_rows(payload: Mapping[str, Any], receipt: str = "") -> list[Row]:
    """One audit row per write attempt in one receipt payload, columns in `AUDIT_COLUMNS` order."""
    meta = mapping(payload.get("meta"))
    frame = mapping(mapping(payload.get("request")).get("frame"))
    dod = mapping(payload.get("definition_of_done"))
    policies = records(payload.get("policies"))
    latest = {str(item.get("check")): item for item in records(payload.get("evidence"))}
    rows: list[Row] = []
    for attempt, entry in _attempts(payload):
        action = mapping(attempt.get("action"))
        verdict = mapping(attempt.get("verdict"))
        action_id = str(action.get("id") or "")
        allowed = bool(verdict.get("allowed"))
        executed = entry is not None and allowed and entry.get("status_code") is not None
        satisfies = strings(action.get("satisfies"))
        target_refs = strings(action.get("target_refs"))
        prefix = f"readback:{action_id}"
        readbacks = {check: item for check, item in latest.items() if check == prefix or check.startswith(f"{prefix}:")}
        if not executed:
            readback = "not_executed"
        elif not readbacks:
            readback = "not_checked"
        else:
            readback = "match" if all(item.get("match") for item in readbacks.values()) else "mismatch"
        row: Row = {
            "timestamp": str(attempt.get("at") or (entry or {}).get("at") or payload.get("generated_at") or ""),
            "trial_id": str(payload.get("trial_id") or ""),
            "receipt": receipt,
            "sequence": (entry or {}).get("sequence"),
            "phase": str(attempt.get("phase") or (entry or {}).get("phase") or ""),
            "requested_by": str(frame.get("reporter") or ""),
            "agent_model": str(meta.get("model") or ""),
            "provider": str(action.get("provider") or (entry or {}).get("provider") or ""),
            "method": str(action.get("method") or (entry or {}).get("method") or ""),
            "path": str(action.get("path") or (entry or {}).get("path") or ""),
            "action_id": action_id,
            "action_kind": str(action.get("kind") or ""),
            "target_refs": list(target_refs),
            "fields_changed": list(strings(action.get("fields"))),
            "gate_decision": "allow" if allowed else "refuse",
            "gate_rule": "" if allowed else str(verdict.get("rule") or ""),
            "gate_reason": str(verdict.get("reason") or ""),
            "executed": executed,
            "status_code": (entry or {}).get("status_code") if executed else None,
            "provider_ok": bool((entry or {}).get("ok")) if executed else None,
            "readback": readback,
            "readback_checks": [
                f"{check}={'match' if item.get('match') else 'mismatch'}" for check, item in readbacks.items()
            ],
            "dod_items": [_dod_item_label(item, dod) for item in satisfies],
            "dod_evidence": [
                f"{item}={'match' if latest[item].get('match') else 'mismatch'}"
                for item in satisfies
                if executed and item in latest
            ],
            "policies": _policies_for(satisfies, target_refs, dod, policies),
            "final_status": str(payload.get("status") or ""),
        }
        rows.append({column: row[column] for column in AUDIT_COLUMNS})
    return rows


def export_rows(paths: Iterable[str | Path]) -> list[Row]:
    rows: list[Row] = []
    for receipt in find_receipts(paths):
        try:
            payload = load_receipt(receipt)
        except ValueError as exc:
            raise AuditError(str(exc)) from exc
        if payload.get("protocol") != RECEIPT_PROTOCOL:
            raise AuditError(f"{receipt}: not a Benchpress receipt (protocol {payload.get('protocol')!r})")
        rows.extend(receipt_rows(payload, str(receipt)))
    return rows


# --------------------------------------------------------------------------------------
# Formats
# --------------------------------------------------------------------------------------


def _csv_cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return LIST_SEPARATOR.join(str(item) for item in cast(list[object], value))
    return str(value)


def render_csv(rows: Sequence[Row]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(AUDIT_COLUMNS)
    for row in rows:
        writer.writerow([_csv_cell(row.get(column)) for column in AUDIT_COLUMNS])
    return buffer.getvalue()


def render_jsonl(rows: Sequence[Row]) -> str:
    return "".join(
        json.dumps({column: row.get(column) for column in AUDIT_COLUMNS}, ensure_ascii=False) + "\n" for row in rows
    )


def export_command(paths: Sequence[str], fmt: str, out: str | None) -> int:
    """`benchpress receipts export`: 0 on success, 2 when a path holds no readable receipt."""
    try:
        rows = export_rows(paths)
    except AuditError as exc:
        print(f"receipts export: {exc}", file=sys.stderr)
        return 2
    text = render_csv(rows) if fmt == "csv" else render_jsonl(rows)
    if out:
        target = Path(out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        print(f"receipts export: {len(rows)} write attempt(s) -> {target}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0
