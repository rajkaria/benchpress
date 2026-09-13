"""Receipts: the auditable record of one run (JSON now; HTML rendered from the same payload)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from benchpress.context import Context


def receipt_payload(ctx: Context, *, meta: Mapping[str, Any]) -> dict[str, Any]:
    latest = ctx.latest_evidence()
    return {
        "protocol": "benchpress-receipt/1",
        "trial_id": ctx.trial_id,
        "status": ctx.status(),
        "meta": dict(meta),
        "request": {
            "prompt": ctx.user_prompt,
            "providers": list(ctx.providers),
            "frame": ctx.frame.model_dump(mode="json"),
        },
        "policies": [policy.model_dump(mode="json") for policy in ctx.policies],
        "candidates": [candidate.model_dump(mode="json") for candidate in ctx.candidates],
        "targets": [target.model_dump(mode="json") for target in ctx.targets],
        "protected": {
            "ids": sorted(ctx.protected.ids),
            "names": sorted(ctx.protected.names),
            "domains": sorted(ctx.protected.domains),
            "emails": sorted(ctx.protected.emails),
        },
        "definition_of_done": ctx.dod.model_dump(mode="json"),
        "plan": [action.model_dump(mode="json") for action in ctx.plan.actions],
        "ledger": [entry.model_dump(mode="json") for entry in ctx.ledger],
        "refusals": [verdict.model_dump(mode="json") for verdict in ctx.refusals],
        "would_refuse": [verdict.model_dump(mode="json") for verdict in ctx.would_refuse],
        "evidence": [item.model_dump(mode="json") for item in latest.values()],
        "evidence_history": [item.model_dump(mode="json") for item in ctx.evidence],
        "deliverables": dict(ctx.deliverable_refs),
        "created": dict(ctx.created),
        "escalation_reason": ctx.escalation_reason,
        "notes": list(ctx.notes),
    }


def write_receipt(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n")


def receipt_summary(payload: Mapping[str, Any]) -> str:
    """A terminal-friendly digest of a receipt."""
    meta = _mapping(payload.get("meta"))
    policies = _records(payload.get("policies"))
    targets = _records(payload.get("targets"))
    protected = _mapping(payload.get("protected"))
    refusals = _records(payload.get("refusals"))
    evidence = _records(payload.get("evidence"))
    notes = [str(note) for note in cast(list[object], payload.get("notes") or [])]
    lines = [f"Benchpress receipt {payload.get('trial_id')} — status {str(payload.get('status')).upper()}"]
    lines.append(
        f"model {meta.get('model')} · provider_api {meta.get('provider_calls')} · docs {meta.get('docs_calls')} · "
        f"{meta.get('latency_ms')} ms · ${meta.get('cost_usd')}"
    )
    lines.append(f"policies found: {len(policies)}")
    for policy in policies[:5]:
        quote = str(policy.get("quote"))[:140]
        lines.append(f"  [{policy.get('kind')}] {policy.get('provider')} {policy.get('resource_ref')}: {quote}")
    lines.append(f"targets: {len(targets)}")
    for target in targets:
        evidence_text = ", ".join(str(item) for item in cast(list[object], target.get("evidence") or []))
        ref = f"{target.get('resource_type')}:{target.get('resource_id')}"
        lines.append(f"  {target.get('provider')} {ref} {target.get('display')} — {evidence_text}")
    names = [str(name) for name in cast(list[object], protected.get("names") or [])]
    ids = cast(list[object], protected.get("ids") or [])
    lines.append(f"protected: {len(ids)} record(s): {', '.join(names[:6])}")
    plan = cast(list[object], payload.get("plan") or [])
    lines.append(f"plan: {len(plan)} action(s); refusals: {len(refusals)}")
    for verdict in refusals:
        lines.append(f"  REFUSED {verdict.get('action_id')} [{verdict.get('rule')}] {verdict.get('reason')}")
    matched = sum(1 for item in evidence if item.get("match"))
    lines.append(f"evidence: {matched}/{len(evidence)} checks match")
    for item in evidence:
        mark = "✓" if item.get("match") else "✗"
        expected = str(item.get("expected"))[:60]
        observed = str(item.get("observed"))[:60]
        lines.append(f"  {mark} {item.get('check')}: expected {expected!r} observed {observed!r}")
    lines.append(f"deliverables: {json.dumps(payload.get('deliverables') or {})}")
    if notes:
        lines.append(f"notes: {len(notes)}")
        lines.extend(f"  - {note}" for note in notes[:8])
    return "\n".join(lines)


def _mapping(value: object) -> dict[str, Any]:
    return dict(cast(Mapping[str, Any], value)) if isinstance(value, Mapping) else {}


def _records(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(cast(Mapping[str, Any], item)) for item in cast(list[object], value) if isinstance(item, Mapping)]
