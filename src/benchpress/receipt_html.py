# ruff: noqa: E501
"""The receipt page: one self-contained HTML file rendered from a receipt payload.

The page is the human-readable twin of `receipt.json`. It is built with the standard
library only, every value passes through `html.escape`, nothing is fetched from the
network and there is no script. The same rule applies as to the JSON: every ✓ and ✗ is
computed from `evidence` (provider state read back), never from the plan or the model.
"""

from __future__ import annotations

import base64
import binascii
import html
import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from string import Template
from typing import Any, cast

from benchpress.tools import MAX_DOCS_CALLS, MAX_PROVIDER_CALLS

PRODUCT = "Benchpress"
TAGLINE = "Every line above is computed from provider state, never from the model's claims."
NO_REFUSALS = "No refusals needed."
ABLATED_HEADING = "Gate ablated: would have refused"

SECTIONS: tuple[tuple[str, str], ...] = (
    ("request", "Request"),
    ("policies", "Policies found"),
    ("candidates", "Candidates"),
    ("definition-of-done", "Definition of done"),
    ("plan", "Plan & execution"),
    ("refused", "Refused"),
    ("deliverables", "Deliverables"),
    ("final-json", "Final JSON"),
)

_STATUS_TONE: Mapping[str, str] = {"completed": "ok", "partial": "warn", "escalated": "accent"}
_KIND_TONE: Mapping[str, str] = {
    "read": "muted",
    "create": "accent",
    "update": "accent",
    "comment": "accent",
    "draft": "warn",
    "message": "accent",
}
_COMPARISON_VERB: Mapping[str, str] = {"email": "is", "text": "equals", "contains": "contains"}

# Deliverable cards: receipt ref key -> (title, badge, definition-of-done kind).
_DELIVERABLE_CARDS: tuple[tuple[str, str, str | None, str], ...] = (
    ("originating_channel_update", "Channel update", None, "originating_channel_update"),
    ("unsent_confirmation", "Customer confirmation draft", "UNSENT", "unsent_customer_confirmation"),
    ("owner_review_record", "Owner review record", None, "owner_review_record"),
)

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_ONE_LINER_MAX = 160

# Sizes are chosen for screen-recording legibility: body 15 px, tables 14 px, headers 20 px/600, nothing under 12 px.
_CSS = """
:root{--ground:#0B0D10;--panel:#12161B;--hairline:#1F2630;--text:#E6EAF0;--muted:#8B96A5;--ok:#3FB950;
--bad:#F85149;--warn:#D29922;--accent:#58A6FF;--sans:ui-sans-serif,Inter,system-ui,sans-serif;
--mono:ui-monospace,"JetBrains Mono",monospace}
*{box-sizing:border-box}
html{background:var(--ground)}
body{margin:0;background:var(--ground);color:var(--text);font:15px/1.5 var(--sans);-webkit-font-smoothing:antialiased}
a{color:var(--accent)}
code,.mono{font-family:var(--mono);font-size:14px}
.wrap{max-width:1080px;margin:0 auto;padding:0 24px}
.top{position:sticky;top:0;z-index:10;background:rgba(11,13,16,.92);backdrop-filter:blur(8px);
border-bottom:1px solid var(--hairline)}
.top-row{display:flex;align-items:center;gap:16px;min-height:64px;padding-top:8px;padding-bottom:8px;flex-wrap:wrap}
.brand{font-size:20px;font-weight:600;letter-spacing:-.01em;white-space:nowrap}
.brand-sub{color:var(--muted);font-weight:500;font-size:14px;margin-left:8px}
.oneliner{flex:1 1 240px;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.metrics{display:flex;align-items:center;gap:16px;flex-wrap:wrap}
.metric{display:inline-flex;align-items:baseline;gap:6px;white-space:nowrap}
.metric .k{color:var(--muted);font-size:13px}
main{padding:24px 0 24px}
.panel{background:var(--panel);border:1px solid var(--hairline);border-radius:8px;padding:24px;margin:0 0 24px}
h2{margin:0 0 16px;font-size:20px;font-weight:600;line-height:32px;display:flex;align-items:center;gap:12px}
.num{display:inline-flex;align-items:center;justify-content:center;width:32px;height:32px;border-radius:8px;
border:1px solid var(--hairline);background:var(--ground);color:var(--accent);font:600 14px/1 var(--mono)}
h3{margin:24px 0 8px;font-size:13px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.08em}
h3:first-of-type{margin-top:0}
.lede{margin:0 0 16px}
.muted{color:var(--muted)}
.empty{color:var(--muted);margin:0}
.sub{color:var(--muted);font-size:13px;margin-top:4px}
.kv{display:grid;grid-template-columns:200px 1fr;gap:8px 24px;margin:0}
.kv dt{color:var(--muted);font-size:14px;padding-top:2px}
.kv dd{margin:0;min-width:0}
.chip{display:inline-block;padding:2px 8px;border-radius:999px;border:1px solid var(--hairline);
background:rgba(255,255,255,.03);font-size:13px;line-height:18px;margin:2px 4px 2px 0;white-space:nowrap}
.chip-ok{color:var(--ok);border-color:rgba(63,185,80,.45);background:rgba(63,185,80,.10)}
.chip-bad{color:var(--bad);border-color:rgba(248,81,73,.45);background:rgba(248,81,73,.10)}
.chip-warn{color:var(--warn);border-color:rgba(210,153,34,.5);background:rgba(210,153,34,.10)}
.chip-accent{color:var(--accent);border-color:rgba(88,166,255,.45);background:rgba(88,166,255,.10)}
.chip-muted{color:var(--muted)}
.pill{display:inline-flex;align-items:center;gap:6px;padding:0 12px;height:24px;border-radius:999px;font-size:13px;
font-weight:600;letter-spacing:.06em;text-transform:uppercase;white-space:nowrap;border:1px solid var(--hairline)}
.pill code{text-transform:none;letter-spacing:0;font-weight:500;font-size:13px;color:inherit}
.pill-ok{color:var(--ok);background:rgba(63,185,80,.14);border-color:rgba(63,185,80,.45)}
.pill-bad{color:var(--bad);background:rgba(248,81,73,.14);border-color:rgba(248,81,73,.45)}
.pill-warn{color:var(--warn);background:rgba(210,153,34,.14);border-color:rgba(210,153,34,.5)}
.pill-accent{color:var(--accent);background:rgba(88,166,255,.14);border-color:rgba(88,166,255,.45)}
.pill-muted{color:var(--muted)}
.badge{display:inline-flex;align-items:center;justify-content:center;width:24px;height:24px;border-radius:6px;
background:rgba(88,166,255,.12);color:var(--accent);font:600 13px/1 var(--mono);margin-right:8px;flex:0 0 auto}
.provider{display:inline-flex;align-items:center;white-space:nowrap;vertical-align:middle}
.policy{padding:16px 0;border-top:1px solid var(--hairline)}
.policy:first-of-type{border-top:0;padding-top:0}
.policy-head{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.quote{margin:8px 0 0;padding:12px 16px;border-left:3px solid var(--warn);background:rgba(210,153,34,.08);
border-radius:0 6px 6px 0}
.policy-suspicious .quote{border-left-color:var(--bad);background:rgba(248,81,73,.08)}
.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:14px}
th{text-align:left;color:var(--muted);font-weight:500;font-size:13px;text-transform:uppercase;letter-spacing:.04em;
padding:8px 12px;border-bottom:1px solid var(--hairline);white-space:nowrap}
td{padding:8px 12px;border-bottom:1px solid var(--hairline);vertical-align:top}
td:first-child,th:first-child{border-left:3px solid transparent}
tr:last-child td{border-bottom:0}
tr.chosen td:first-child{border-left-color:var(--ok)}
tr.ok td:first-child{border-left-color:var(--ok)}
tr.protected{color:var(--bad)}
tr.protected td:first-child{border-left-color:var(--bad)}
tr.refused td:first-child{border-left-color:var(--bad)}
.lock{margin-right:4px}
.evidence{margin-top:4px}
.checks{margin:0;padding:0;list-style:none}
.check{display:grid;grid-template-columns:32px 1fr;gap:0 8px;padding:8px 0;border-top:1px solid var(--hairline)}
.check:first-child{border-top:0}
.mark{font-size:18px;line-height:24px;text-align:center;font-weight:700}
.mark-ok{color:var(--ok)}
.mark-bad{color:var(--bad)}
.mark-muted{color:var(--muted)}
.check-label{line-height:24px}
.check-id{color:var(--muted);font-size:13px;margin-left:8px}
.check-detail{color:var(--muted);font-size:14px;margin-top:2px}
.rb{display:block;font-family:var(--mono);font-size:14px;word-break:break-word}
.exp{color:var(--muted)}
.arrow{color:var(--muted);margin:0 6px}
.obs-ok{color:var(--ok)}
.obs-bad{color:var(--bad);background:rgba(248,81,73,.14);border-radius:4px;padding:0 4px}
.status-ok{color:var(--ok)}
.status-bad{color:var(--bad)}
.req{font-family:var(--mono);font-size:14px;word-break:break-word}
.req b{font-weight:600}
.callout{padding:12px 16px;border-radius:6px;border:1px solid var(--hairline);margin:16px 0 0}
.callout-accent{border-color:rgba(88,166,255,.45);background:rgba(88,166,255,.08)}
.callout-warn{border-color:rgba(210,153,34,.5);background:rgba(210,153,34,.08)}
.callout-bad{border-color:rgba(248,81,73,.45);background:rgba(248,81,73,.08)}
.refusal{padding:12px 0;border-top:1px solid var(--hairline)}
.refusal:first-of-type{border-top:0;padding-top:0}
.refusal-head{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.refusal p{margin:4px 0 0}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}
.card{border:1px solid var(--hairline);border-radius:8px;padding:16px;background:var(--ground)}
.card-head{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:8px}
.card-title{font-weight:600}
.card .ref{display:block;margin:4px 0 8px;word-break:break-all}
.preview{margin:8px 0 0;padding:12px;background:var(--panel);border:1px solid var(--hairline);border-radius:6px;
font:13px/1.5 var(--mono);white-space:pre-wrap;word-break:break-word;max-height:240px;overflow:auto;color:var(--text)}
details{border:1px solid var(--hairline);border-radius:8px;background:var(--ground)}
summary{cursor:pointer;padding:12px 16px;color:var(--accent);font-weight:600;list-style:none}
summary::-webkit-details-marker{display:none}
summary::before{content:"\\25B8";display:inline-block;margin-right:8px;transition:transform .15s}
details[open] summary::before{transform:rotate(90deg)}
pre{margin:0;padding:16px;border-top:1px solid var(--hairline);font:14px/1.5 var(--mono);overflow-x:auto;white-space:pre}
pre.pre-wrap{white-space:pre-wrap;word-break:break-word}
.ledger-line{margin:16px 0 0;color:var(--muted);font-size:14px}
.foot{padding:24px 24px 64px;color:var(--muted);font-size:14px;border-top:1px solid var(--hairline)}
.notes{margin:8px 0 0;padding-left:20px}
.tagline{margin:16px 0 0;color:var(--text);font-weight:500}
@media (max-width:720px){.kv{grid-template-columns:1fr}.panel{padding:16px}.top-row{gap:8px}}
"""

_PAGE = Template(
    """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>$title</title>
<style>$css</style>
</head>
<body>
$header
<main class="wrap">
$sections
</main>
$footer
</body>
</html>
"""
)


# --------------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------------


def render_receipt_html(payload: Mapping[str, Any]) -> str:
    """Render one receipt payload (the dict `receipt_payload` produces, or `receipt.json` loaded) to HTML."""
    receipt = _Receipt(payload)
    renderers = (
        _section_request,
        _section_policies,
        _section_candidates,
        _section_definition_of_done,
        _section_plan,
        _section_refused,
        _section_deliverables,
        _section_final_json,
    )
    sections = "\n".join(
        _panel(index, anchor, title, render(receipt))
        for index, ((anchor, title), render) in enumerate(zip(SECTIONS, renderers, strict=True), start=1)
    )
    return _PAGE.substitute(
        title=_esc(f"{PRODUCT} receipt · {receipt.trial_id} · {receipt.status.upper()}"),
        css=_CSS,
        header=_header(receipt),
        sections=sections,
        footer=_footer(receipt),
    )


def write_receipt_html(receipt_json: Path, out: Path) -> Path:
    """Read `receipt.json`, render it, write the page to `out` and return that path."""
    raw: object = json.loads(Path(receipt_json).read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError(f"{receipt_json} does not hold a receipt object")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_receipt_html(_mapping(cast(object, raw))), encoding="utf-8")
    return out


# --------------------------------------------------------------------------------------
# Typed view over the payload
# --------------------------------------------------------------------------------------


class _Receipt:
    """Every field the page needs, coerced once, with empty defaults for partial receipts."""

    def __init__(self, payload: Mapping[str, Any]) -> None:
        self.trial_id = _text(payload.get("trial_id")) or "—"
        self.protocol = _text(payload.get("protocol"))
        self.status = (_text(payload.get("status")) or "partial").lower()
        self.meta = _mapping(payload.get("meta"))
        self.ablations = _strings(self.meta.get("ablations"))
        request = _mapping(payload.get("request"))
        self.prompt = _text(request.get("prompt"))
        self.providers = _strings(request.get("providers"))
        self.frame = _mapping(request.get("frame"))
        self.policies = _records(payload.get("policies"))
        self.candidates = _records(payload.get("candidates"))
        self.targets = _records(payload.get("targets"))
        protected = _mapping(payload.get("protected"))
        self.protected_ids = set(_strings(protected.get("ids")))
        self.protected_names = {name.casefold() for name in _strings(protected.get("names"))}
        self.protected_domains = {domain.casefold() for domain in _strings(protected.get("domains"))}
        self.protected_emails = {email.casefold() for email in _strings(protected.get("emails"))}
        self.protected_terms = (
            ("id", _strings(protected.get("ids"))),
            ("name", _strings(protected.get("names"))),
            ("domain", _strings(protected.get("domains"))),
            ("email", _strings(protected.get("emails"))),
        )
        self.dod = _mapping(payload.get("definition_of_done"))
        self.plan = _records(payload.get("plan"))
        self.ledger = _records(payload.get("ledger"))
        self.refusals = _records(payload.get("refusals"))
        self.would_refuse = _records(payload.get("would_refuse"))
        self.evidence = _records(payload.get("evidence"))
        self.by_check: dict[str, dict[str, Any]] = {}
        for item in self.evidence:
            self.by_check[_text(item.get("check"))] = item
        self.deliverables = {str(key): _text(value) for key, value in _mapping(payload.get("deliverables")).items()}
        self.created = {str(key): _text(value) for key, value in _mapping(payload.get("created")).items()}
        self.notes = _strings(payload.get("notes"))
        self.escalation_reason = _text(payload.get("escalation_reason"))
        self.target_by_key = {
            (_text(t.get("provider")), _text(t.get("resource_type")), _text(t.get("resource_id"))): t
            for t in self.targets
        }
        self.plan_by_id = {_text(action.get("id")): action for action in self.plan}

    # -- lookups -------------------------------------------------------------------------

    def is_protected(self, candidate: Mapping[str, Any]) -> bool:
        if _text(candidate.get("resource_id")) in self.protected_ids:
            return True
        for key in ("display", "name"):
            value = _text(candidate.get(key)).casefold()
            if value and value in self.protected_names:
                return True
        domain = _text(candidate.get("domain")).casefold()
        email = _text(candidate.get("email")).casefold()
        return bool(domain and domain in self.protected_domains) or bool(email and email in self.protected_emails)

    def ledger_for(self, action_id: str) -> list[dict[str, Any]]:
        return [entry for entry in self.ledger if _text(entry.get("action_id")) == action_id]

    def refusal_for(self, action_id: str) -> dict[str, Any] | None:
        return next((v for v in self.refusals if _text(v.get("action_id")) == action_id), None)

    def readbacks_for(self, action_id: str) -> list[dict[str, Any]]:
        prefixes = (f"readback:{action_id}:", f"readback:{action_id}", f"write:{action_id}")
        return [item for check, item in self.by_check.items() if check.startswith(prefixes)]

    def action_for_ref(self, ref: str) -> dict[str, Any] | None:
        """The planned action that produced a deliverable ref, matched through `created`."""
        for action_id, created in self.created.items():
            if created and (ref == created or ref.endswith(f":{created}")):
                return self.plan_by_id.get(action_id)
        return None


# --------------------------------------------------------------------------------------
# Header and footer
# --------------------------------------------------------------------------------------


def _header(r: _Receipt) -> str:
    tone = _STATUS_TONE.get(r.status, "warn")
    metrics = [
        _pill(r.status.upper(), tone),
        _metric("provider_api", f"{_int_text(r.meta.get('provider_calls'))}/{MAX_PROVIDER_CALLS}"),
        _metric("elapsed", _elapsed(r.meta.get("latency_ms"))),
        _metric("cost", _cost(r.meta.get("cost_usd"))),
        _metric("model", _text(r.meta.get("model")) or "—"),
    ]
    metrics.extend(_chip(f"ablated: {name}", "warn") for name in r.ablations)
    error = _text(r.meta.get("error"))
    if error:
        metrics.append(_chip(f"error: {error}", "bad"))
    one_liner = _one_liner(r)
    return (
        '<header class="top"><div class="wrap top-row">'
        f'<div class="brand">{_esc(PRODUCT)}<span class="brand-sub">receipt</span></div>'
        f'<div class="oneliner" title="{_esc(r.prompt or one_liner)}">{_esc(one_liner)}</div>'
        f'<div class="metrics">{"".join(metrics)}</div>'
        "</div></header>"
    )


def _footer(r: _Receipt) -> str:
    meta = r.meta
    provider_calls_text = _mono(f"{_int_text(meta.get('provider_calls'))}/{MAX_PROVIDER_CALLS}")
    docs_calls_text = _mono(f"{_int_text(meta.get('docs_calls'))}/{MAX_DOCS_CALLS}")
    parts = [
        f"trial {_mono(r.trial_id)}",
        f"protocol {_mono(r.protocol or '—')}",
        f"model {_mono(_text(meta.get('model')) or '—')}"
        + (f" via {_esc(meta.get('model_provider'))}" if _text(meta.get("model_provider")) else ""),
        f"provider_api {provider_calls_text}",
        f"provider_docs {docs_calls_text}",
        f"model calls {_mono(_int_text(meta.get('model_calls')))}",
        f"elapsed {_mono(_elapsed(meta.get('latency_ms')))}",
        f"cost {_mono(_cost(meta.get('cost_usd')))}",
    ]
    notes = ""
    if r.notes:
        notes = '<ul class="notes">' + "".join(f"<li>{_esc(note)}</li>" for note in r.notes) + "</ul>"
    return (
        '<footer class="foot wrap">'
        f'<div class="foot-meta">{" · ".join(parts)}</div>'
        f"{notes}"
        f'<p class="tagline">{_esc(TAGLINE)}</p>'
        "</footer>"
    )


def _one_liner(r: _Receipt) -> str:
    requested = _text(r.frame.get("requested_change")).strip()
    text = requested or _first_sentence(r.prompt) or "(no request text)"
    text = text[:1].upper() + text[1:]
    return text if len(text) <= _ONE_LINER_MAX else text[: _ONE_LINER_MAX - 1].rstrip() + "…"


def _first_sentence(text: str) -> str:
    flat = " ".join(text.split())
    return _SENTENCE_END.split(flat, maxsplit=1)[0] if flat else ""


# --------------------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------------------


def _section_request(r: _Receipt) -> str:
    frame = r.frame
    rows: list[str] = []
    channel = _text(frame.get("originating_channel"))
    rows.append(_kv("Originating channel", _mono(channel) if channel else _muted("not stated")))
    reporter = _text(frame.get("reporter"))
    rows.append(_kv("Reporter", _esc(reporter) if reporter else _muted("not stated")))
    role = _text(frame.get("role"))
    if role:
        rows.append(_kv("Role", _esc(role)))
    requested = _text(frame.get("requested_change"))
    rows.append(_kv("Requested change", _esc(requested) if requested else _muted("not parsed")))
    entities = _strings(frame.get("subject_entities"))
    if entities:
        rows.append(_kv("Subject entities", _chips(entities, "accent")))
    prohibitions = _strings(frame.get("explicit_prohibitions"))
    rows.append(_kv("Prohibitions", _chips(prohibitions, "bad") if prohibitions else _muted("none stated")))
    hint = _text(frame.get("distractor_hint"))
    if hint:
        rows.append(_kv("Distractor hint", _esc(hint)))
    rows.append(_kv("Providers", " ".join(_provider(p) for p in r.providers) if r.providers else _muted("none")))
    body = f'<dl class="kv">{"".join(rows)}</dl>'
    if r.prompt:
        body += _details("Full prompt", f'<pre class="pre-wrap">{_esc(r.prompt)}</pre>', extra_class="prompt")
    return body


def _section_policies(r: _Receipt) -> str:
    if not r.policies:
        note = " " + _chip("policy sweep ablated", "warn") if "no_policy_sweep" in r.ablations else ""
        return _empty("No operating rules were found in the provisioned systems.") + note
    items: list[str] = []
    for policy in r.policies:
        kind = _text(policy.get("kind")) or "other"
        suspicious = kind == "suspicious"
        head = (
            '<div class="policy-head">'
            f"{_provider(_text(policy.get('provider')))}"
            f"{_mono(_text(policy.get('resource_ref')) or '—')}"
            f"{_chip(kind.replace('_', ' '), 'bad' if suspicious else 'warn')}"
            "</div>"
        )
        quote = f'<blockquote class="quote">{_esc(_text(policy.get("quote")))}</blockquote>'
        applies = _strings(policy.get("applies_to"))
        tail = f'<div class="sub">applies to {_chips(applies, "muted")}</div>' if applies else ""
        css = "policy policy-suspicious" if suspicious else "policy"
        items.append(f'<article class="{css}">{head}{quote}{tail}</article>')
    return "".join(items)


def _section_candidates(r: _Receipt) -> str:
    parts: list[str] = []
    if not r.candidates:
        parts.append(_empty("No candidates were enumerated."))
    else:
        rows: list[str] = []
        for candidate in r.candidates:
            key = (
                _text(candidate.get("provider")),
                _text(candidate.get("resource_type")),
                _text(candidate.get("resource_id")),
            )
            target = r.target_by_key.get(key)
            protected = target is None and r.is_protected(candidate)
            css = "chosen" if target is not None else ("protected" if protected else "")
            display = _esc(_text(candidate.get("display")))
            notes = _text(candidate.get("notes"))
            if notes:
                display += f'<div class="sub" title="{_esc(notes)}">{_esc(_clip(notes, 120))}</div>'
            if target is not None:
                evidence = _strings(target.get("evidence"))
                confidence = _text(target.get("confidence")) or "high"
                status = f'<span class="mark-ok">✓</span> chosen · confidence {_esc(confidence)}'
                if evidence:
                    display += f'<div class="evidence">{_chips(evidence, "ok")}</div>'
            elif protected:
                status = '<span class="lock" role="img" aria-label="protected">🔒</span> protected'
            else:
                status = _muted("unresolved")
            contact = [
                _mono(value) for value in (_text(candidate.get("domain")), _text(candidate.get("email"))) if value
            ]
            rows.append(
                f'<tr class="{css}">'
                f"<td>{_provider(key[0])}</td>"
                f"<td>{_esc(key[1])}</td>"
                f"<td>{_mono(key[2])}</td>"
                f"<td>{display}</td>"
                f"<td>{_esc(_text(candidate.get('lifecycle')) or '—')}</td>"
                f"<td>{'<br>'.join(contact) or _muted('—')}</td>"
                f"<td>{status}</td>"
                "</tr>"
            )
        parts.append(_table(("Provider", "Type", "Id", "Display", "Lifecycle", "Domain / email", "Status"), rows))
    locked = [(label, values) for label, values in r.protected_terms if values]
    if locked:
        chips = "".join(
            _chip(f"{value}", "bad", title=f"protected {label}") for label, values in locked for value in values
        )
        parts.append(
            '<div class="callout callout-bad">'
            '<span class="lock" role="img" aria-label="protected">🔒</span>'
            "<strong>Protected set</strong> — every write is checked against these terms in code: "
            f"{chips}</div>"
        )
    if r.status == "escalated" or r.escalation_reason:
        reason = r.escalation_reason or "ambiguous target; no write was made"
        parts.append(f'<div class="callout callout-accent"><strong>Escalated</strong> — {_esc(reason)}</div>')
    return "".join(parts)


def _section_definition_of_done(r: _Receipt) -> str:
    dod = r.dod
    parts: list[str] = []
    summary = _text(dod.get("summary"))
    if summary:
        parts.append(f'<p class="lede">{_esc(summary)}</p>')
    used: set[str] = set()

    end_state = _records(dod.get("end_state"))
    parts.append("<h3>End state</h3>")
    if end_state:
        rows: list[str] = []
        for index, item in enumerate(end_state):
            check = f"end_state[{index}]"
            used.add(check)
            verb = _COMPARISON_VERB.get(_text(item.get("comparison")), _text(item.get("comparison")) or "contains")
            label = (
                f"{_provider(_text(item.get('provider')))}{_mono(_text(item.get('resource')))} · "
                f"{_esc(_text(item.get('field')))} {_esc(verb)} {_mono(_text(item.get('expected')))}"
            )
            rows.append(_check_row(r.by_check.get(check), label, check))
        parts.append(f'<ul class="checks">{"".join(rows)}</ul>')
    else:
        parts.append(_empty("No end-state items were defined."))

    deliverables = _records(dod.get("deliverables"))
    parts.append("<h3>Deliverables</h3>")
    if deliverables:
        rows = []
        for item in deliverables:
            kind = _text(item.get("kind"))
            check = f"deliverable:{kind}"
            used.add(check)
            label = _esc(_humanize(kind))
            where = [_provider(_text(item.get("provider")))] if _text(item.get("provider")) else []
            channel = _text(item.get("channel"))
            if channel:
                where.append(_mono(channel))
            if where:
                label += " · " + " ".join(where)
            because = _text(item.get("because"))
            detail = f"because {_mono(because)}" if because else ""
            mentions = _dedupe(_strings(item.get("must_mention")))
            if mentions:
                detail += (" · " if detail else "") + "must mention " + _chips(mentions, "muted")
            if kind == "structured_result":
                rows.append(_check_row(None, label, check, detail=detail, neutral="the evidence-only JSON in §8"))
            else:
                rows.append(_check_row(r.by_check.get(check), label, check, detail=detail))
        parts.append(f'<ul class="checks">{"".join(rows)}</ul>')
    else:
        parts.append(_empty("No deliverables were defined."))

    audits = [
        (check, item)
        for check, item in r.by_check.items()
        if check not in used and not check.startswith(("readback:", "write:"))
    ]
    if audits:
        parts.append("<h3>State audits</h3>")
        rows = []
        for check, item in audits:
            label = f"{_provider(_text(item.get('provider')))}{_mono(_text(item.get('resource')))}"
            label += f" · expected {_esc(_text(item.get('expected')))}"
            rows.append(_check_row(item, label, check))
        parts.append(f'<ul class="checks">{"".join(rows)}</ul>')

    forbidden = _strings(dod.get("forbidden"))
    parts.append("<h3>Forbidden classes</h3>")
    parts.append(_chips(forbidden, "bad") if forbidden else _empty("None declared."))
    scope = _strings(dod.get("write_scope"))
    if scope:
        parts.append("<h3>Write scope</h3>")
        parts.append(" ".join(_provider(p) for p in scope))

    facts = [(str(key), _text(value)) for key, value in _mapping(dod.get("facts")).items()]
    for label, key in (("account owner", "account_owner"), ("customer contact", "customer_contact")):
        value = _text(dod.get(key))
        if value:
            facts.append((label, value))
    parts.append("<h3>Facts</h3>")
    if facts:
        rows = [f"<tr><td>{_esc(key)}</td><td>{_mono(value)}</td></tr>" for key, value in facts]
        parts.append(_table(("Fact", "Value"), rows))
    else:
        parts.append(_empty("No facts recorded."))
    escalation = _text(dod.get("escalation"))
    if escalation:
        parts.append(f'<div class="callout callout-accent"><strong>Escalation rule</strong> — {_esc(escalation)}</div>')
    return "".join(parts)


def _section_plan(r: _Receipt) -> str:
    rows: list[str] = []
    for action in r.plan:
        action_id = _text(action.get("id"))
        entries = r.ledger_for(action_id)
        verdicts = [_mapping(entry.get("gate")) for entry in entries if isinstance(entry.get("gate"), Mapping)]
        refusal = next((v for v in verdicts if v.get("allowed") is False), None) or r.refusal_for(action_id)
        allowed = next((v for v in verdicts if v.get("allowed") is True), None)
        if refusal is not None:
            verdict, css = _pill_refused(_text(refusal.get("rule")), title=_text(refusal.get("reason"))), "refused"
        elif allowed is not None and _text(allowed.get("rule")) == "ablated":
            verdict, css = _pill("ablated", "warn"), ""
        elif allowed is not None:
            verdict, css = _pill("allowed", "ok"), "ok"
        elif _text(action.get("method")).upper() == "GET":
            verdict, css = _pill("read", "muted"), ""
        else:
            verdict, css = _pill("not executed", "muted"), ""
        rows.append(
            f'<tr class="{css}">'
            f"<td>{_mono(action_id)}</td>"
            f"<td>{_kind_chip(_text(action.get('kind')))}</td>"
            f"<td>{_provider(_text(action.get('provider')))}</td>"
            f"<td>{_request_cell(action)}</td>"
            f"<td>{verdict}</td>"
            f"<td>{_http_cell(entries)}</td>"
            f"<td>{_readback_cell(r, action_id)}</td>"
            "</tr>"
        )
    for verdict in r.refusals:
        action_id = _text(verdict.get("action_id"))
        if action_id in r.plan_by_id:
            continue
        rows.append(
            '<tr class="refused">'
            f"<td>{_mono(action_id)}</td>"
            f"<td>{_chip('dropped', 'bad')}</td>"
            f"<td>{_muted('—')}</td>"
            f"<td>{_muted('never planned — refused before execution')}</td>"
            f"<td>{_pill_refused(_text(verdict.get('rule')), title=_text(verdict.get('reason')))}</td>"
            f"<td>{_muted('—')}</td>"
            f"<td>{_muted('—')}</td>"
            "</tr>"
        )
    if not rows:
        return _empty("No actions were planned.") + _ledger_line(r)
    table = _table(("#", "Kind", "Provider", "Request", "Gate", "HTTP", "Read-back"), rows)
    return table + _ledger_line(r)


def _section_refused(r: _Receipt) -> str:
    parts: list[str] = []
    if r.refusals:
        parts.extend(_refusal_item(r, verdict, _pill_refused(_text(verdict.get("rule")))) for verdict in r.refusals)
    else:
        parts.append(f'<p class="empty"><span class="mark-ok">✓</span> {_esc(NO_REFUSALS)}</p>')
    if r.would_refuse:
        parts.append(f"<h3>{_esc(ABLATED_HEADING)}</h3>")
        parts.append(
            '<div class="callout callout-warn">The gate was switched off for this run (ablation). '
            "These writes reached the provider; with the gate on, each would have been refused.</div>"
        )
        parts.extend(
            _refusal_item(r, verdict, _pill_refused(_text(verdict.get("rule")), label="would refuse", tone="warn"))
            for verdict in r.would_refuse
        )
    return "".join(parts)


def _section_deliverables(r: _Receipt) -> str:
    cards: list[str] = []
    dod_kinds = {_text(item.get("kind")) for item in _records(r.dod.get("deliverables"))}
    seen: set[str] = set()
    for key, title, badge, kind in _DELIVERABLE_CARDS:
        seen.add(key)
        ref = r.deliverables.get(key, "")
        required = kind in dod_kinds
        if not ref and not required:
            continue
        cards.append(_card(r, title, badge, ref, r.by_check.get(f"deliverable:{kind}"), required))
    for key, ref in r.deliverables.items():
        if key in seen:
            continue
        cards.append(_card(r, _humanize(key), None, ref, r.by_check.get(f"deliverable:{key}"), True))
    if not cards:
        return _empty("No deliverables were produced.")
    return f'<div class="cards">{"".join(cards)}</div>'


def _section_final_json(r: _Receipt) -> str:
    final = {
        "status": r.status,
        "trial_id": r.trial_id,
        "facts": _mapping(r.dod.get("facts")),
        "deliverables": r.deliverables,
        "evidence": r.evidence,
    }
    text = json.dumps(final, ensure_ascii=False, indent=2, default=str)
    return _details("Final JSON — evidence, deliverables, facts", f"<pre>{_esc(text)}</pre>")


# --------------------------------------------------------------------------------------
# Section helpers
# --------------------------------------------------------------------------------------


def _panel(index: int, anchor: str, title: str, body: str) -> str:
    return (
        f'<section class="panel" id="{_esc(anchor)}">'
        f'<h2><span class="num">{index}</span>{_esc(title)}</h2>'
        f"{body}</section>"
    )


def _check_row(
    item: Mapping[str, Any] | None,
    label: str,
    check: str,
    *,
    detail: str = "",
    neutral: str | None = None,
) -> str:
    if neutral is not None:
        mark, tone, observed = "–", "muted", neutral
    elif item is None:
        mark, tone, observed = "✗", "muted", "no evidence recorded — not verified"
    elif bool(item.get("match")):
        mark, tone, observed = "✓", "ok", f"observed {_mono(_text(item.get('observed')))}"
    else:
        mark, tone, observed = "✗", "bad", f"observed {_mono(_text(item.get('observed')) or '(nothing)')}"
    if item is not None and neutral is None:
        extra = _text(item.get("detail"))
        if extra:
            observed += f" · {_esc(extra)}"
    detail_html = f"{detail} · {observed}" if detail else observed
    return (
        f'<li class="check check-{tone}">'
        f'<span class="mark mark-{tone}">{mark}</span>'
        f'<div><div class="check-label">{label}<code class="check-id">{_esc(check)}</code></div>'
        f'<div class="check-detail">{detail_html}</div></div>'
        "</li>"
    )


def _request_cell(action: Mapping[str, Any]) -> str:
    method = _text(action.get("method")).upper() or "—"
    path = _text(action.get("path"))
    query = _mapping(action.get("query"))
    if query:
        path += "?" + "&".join(f"{key}={value}" for key, value in query.items())
    cell = f'<div class="req"><b>{_esc(method)}</b> {_esc(path)}</div>'
    subs: list[str] = []
    satisfies = _strings(action.get("satisfies"))
    if satisfies:
        subs.append("satisfies " + _chips(satisfies, "muted"))
    fields = _strings(action.get("fields"))
    if fields:
        subs.append("fields " + _chips(fields, "muted"))
    rationale = _text(action.get("rationale"))
    if rationale:
        subs.append(_esc(_clip(rationale, 140)))
    if subs:
        cell += f'<div class="sub">{" · ".join(subs)}</div>'
    return cell


def _http_cell(entries: Sequence[Mapping[str, Any]]) -> str:
    attempts = [entry for entry in entries if entry.get("status_code") is not None or _text(entry.get("error"))]
    if not attempts:
        return _muted("—")
    last = attempts[-1]
    code = last.get("status_code")
    error = _text(last.get("error"))
    if isinstance(code, int):
        tone = "ok" if 200 <= code < 300 else "bad"
        text = f'<code class="status-{tone}">{code}</code>'
        return text + (f'<div class="sub">{_esc(error)}</div>' if error else "")
    return f'<code class="status-bad">—</code><div class="sub">{_esc(error or "no response")}</div>'


def _readback_cell(r: _Receipt, action_id: str) -> str:
    items = r.readbacks_for(action_id)
    if not items:
        if "no_readback" in r.ablations:
            return _chip("read-back ablated", "warn")
        return _muted("—")
    lines: list[str] = []
    for item in items:
        check = _text(item.get("check"))
        matched = bool(item.get("match"))
        mark = '<span class="mark-ok">✓</span>' if matched else '<span class="mark-bad">✗</span>'
        field = check.split(":", 2)[2] if check.count(":") >= 2 else ""
        expected = _text(item.get("expected"))
        observed = _text(item.get("observed"))
        obs_class = "obs-ok" if matched else "obs-bad"
        prefix = f"{_esc(field)}: " if field else ""
        if check.startswith("write:"):
            prefix = "write: "
        lines.append(
            f'<span class="rb">{mark} {prefix}<span class="exp">{_esc(expected)}</span>'
            f'<span class="arrow">→</span><span class="{obs_class}">{_esc(observed) or "(nothing)"}</span></span>'
        )
    return "".join(lines)


def _ledger_line(r: _Receipt) -> str:
    if not r.ledger:
        return ""
    phases = Counter(_text(entry.get("phase")) or "?" for entry in r.ledger)
    writes = sum(1 for entry in r.ledger if _text(entry.get("method")).upper() != "GET")
    errors = sum(1 for entry in r.ledger if not bool(entry.get("ok")))
    per_phase = " · ".join(f"{_esc(phase)} {count}" for phase, count in phases.items())
    return (
        f'<p class="ledger-line">Ledger: {len(r.ledger)} provider calls — {writes} non-GET, '
        f"{len(r.ledger) - writes} reads, {errors} without a 2xx · {per_phase}</p>"
    )


def _refusal_item(r: _Receipt, verdict: Mapping[str, Any], pill: str) -> str:
    action_id = _text(verdict.get("action_id"))
    action = r.plan_by_id.get(action_id)
    request = ""
    if action is not None:
        request = (
            f'<div class="req"><b>{_esc(_text(action.get("method")).upper())}</b> '
            f"{_esc(_text(action.get('path')))}</div>"
        )
    reason = _text(verdict.get("reason")) or "no reason recorded"
    return (
        '<article class="refusal">'
        f'<div class="refusal-head">{_mono(action_id or "—")}{pill}</div>'
        f"{request}<p>{_esc(reason)}</p></article>"
    )


def _card(r: _Receipt, title: str, badge: str | None, ref: str, item: Mapping[str, Any] | None, required: bool) -> str:
    head = f'<span class="card-title">{_esc(title)}</span>'
    if badge:
        head += _pill(badge, "warn")
    if ref:
        ref_html = f'<code class="ref">{_esc(ref)}</code>'
    else:
        ref_html = f'<span class="ref muted">{"not produced" if required else "not required"}</span>'
    if item is None:
        evidence = _muted("no presence check recorded")
    else:
        matched = bool(item.get("match"))
        mark = '<span class="mark-ok">✓</span>' if matched else '<span class="mark-bad">✗</span>'
        evidence = f'{mark} {_esc(_text(item.get("observed")))} <span class="muted">· expected {_esc(_text(item.get("expected")))}</span>'  # noqa: E501
    preview = _preview(r.action_for_ref(ref)) if ref else ""
    return (
        f'<article class="card"><div class="card-head">{head}</div>{ref_html}<div>{evidence}</div>{preview}</article>'
    )


def _preview(action: Mapping[str, Any] | None) -> str:
    if action is None:
        return ""
    body = _mapping(action.get("body"))
    text = _text(body.get("text"))
    if not text:
        raw = _text(_mapping(body.get("message")).get("raw"))
        text = _decode_raw(raw) if raw else ""
    if not text:
        return ""
    return f'<pre class="preview">{_esc(text)}</pre>'


def _decode_raw(raw: str) -> str:
    try:
        decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except (binascii.Error, ValueError):
        return ""
    return decoded.decode("utf-8", errors="replace").replace("\r\n", "\n")


# --------------------------------------------------------------------------------------
# Fragments (every dynamic value is escaped here)
# --------------------------------------------------------------------------------------


def _esc(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _mono(value: object) -> str:
    return f'<code class="mono">{_esc(value)}</code>'


def _muted(text: str) -> str:
    return f'<span class="muted">{_esc(text)}</span>'


def _chip(text: object, tone: str = "muted", *, title: str = "") -> str:
    attr = f' title="{_esc(title)}"' if title else ""
    return f'<span class="chip chip-{_esc(tone)}"{attr}>{_esc(text)}</span>'


def _chips(values: Iterable[object], tone: str) -> str:
    return "".join(_chip(value, tone) for value in values)


def _kind_chip(kind: str) -> str:
    return _chip(kind or "?", _KIND_TONE.get(kind, "muted"))


def _pill(text: object, tone: str) -> str:
    return f'<span class="pill pill-{_esc(tone)}">{_esc(text)}</span>'


def _pill_refused(rule: str, *, label: str = "refused", tone: str = "bad", title: str = "") -> str:
    attr = f' title="{_esc(title)}"' if title else ""
    return f'<span class="pill pill-{_esc(tone)}"{attr}>{_esc(label)}<code>{_esc(rule or "rule")}</code></span>'


def _provider(name: str) -> str:
    if not name:
        return _muted("—")
    initial = name[:1].upper()
    return f'<span class="provider"><span class="badge">{_esc(initial)}</span>{_esc(name)}</span>'


def _kv(label: str, value_html: str) -> str:
    return f"<div><dt>{_esc(label)}</dt></div><div><dd>{value_html}</dd></div>"


def _table(headers: Sequence[str], rows: Sequence[str]) -> str:
    head = "".join(f"<th>{_esc(header)}</th>" for header in headers)
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'


def _details(summary: str, body_html: str, *, extra_class: str = "") -> str:
    css = f' class="{_esc(extra_class)}"' if extra_class else ""
    return f"<details{css}><summary>{_esc(summary)}</summary>{body_html}</details>"


def _empty(text: str) -> str:
    return f'<p class="empty">{_esc(text)}</p>'


def _metric(key: str, value: str) -> str:
    return f'<span class="metric"><span class="k">{_esc(key)}</span>{_mono(value)}</span>'


# --------------------------------------------------------------------------------------
# Coercion
# --------------------------------------------------------------------------------------


def _mapping(value: object) -> dict[str, Any]:
    if isinstance(value, Mapping):
        typed = cast(Mapping[object, object], value)
        return {str(key): item for key, item in typed.items()}
    return {}


def _records(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    items = cast(Sequence[object], value)
    return [_mapping(cast(object, item)) for item in items if isinstance(item, Mapping)]


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    items = cast(Iterable[object], value)
    return [str(item) for item in items if item is not None and str(item)]


def _text(value: object) -> str:
    return "" if value is None else str(value)


def _int_text(value: object) -> str:
    if isinstance(value, bool):
        return "—"
    if isinstance(value, (int, float)):
        return str(int(value))
    return "—"


def _elapsed(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "—"
    ms = float(value)
    if ms < 1000:
        return f"{ms:.0f} ms"
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes, rest = divmod(round(seconds), 60)
    return f"{minutes}m {rest:02d}s"


def _cost(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "—"
    cost = float(value)
    return f"${cost:.4f}" if cost < 1 else f"${cost:.2f}"


def _humanize(kind: str) -> str:
    text = kind.replace("_", " ").strip()
    return text[:1].upper() + text[1:] if text else "—"


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out
