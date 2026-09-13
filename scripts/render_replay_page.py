# ruff: noqa: E501
"""Render `docs/demo/index.html`: a self-contained, step-through replay of one real Benchpress run.

    uv run benchpress demo --trace-dir runs/page-demo
    uv run python scripts/render_replay_page.py --run-dir runs/page-demo --out docs/demo/index.html

Inputs are the run's `receipt.json` and `benchpress-trace.jsonl`. Every value on the page is read
from those two files and passes through `html.escape`; the renderer invents no numbers and no text
beyond fixed labels. The page loads nothing from the network (no fonts, no CDN, no images): the only
`https://` strings in it are plain `<a href>` links in the footer.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import html
import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

PROMISE = "The model proposes; code decides every write."
INSTALL = "uvx --from benchpress-agent benchpress demo"
SUBSTRATE = "In-memory workspace, scripted model: the same loop, gate and read-back as production. No keys, no network."
GITHUB = "https://github.com/rajkaria/benchpress"
LINKS: tuple[tuple[str, str], ...] = (
    ("GitHub", GITHUB),
    ("PyPI", "https://pypi.org/project/benchpress-agent/"),
    ("CHANGELOG", f"{GITHUB}/blob/main/CHANGELOG.md"),
    ("reports/summary.md", f"{GITHUB}/blob/main/reports/summary.md"),
)

# (id, rail label, panel title, what code owns in this phase). Labels follow README §4.
PHASES: tuple[tuple[str, str, str, str], ...] = (
    ("P0", "orient", "Orient", "Parse the request into a typed task frame; read the originating channel."),
    ("P1", "policy", "Policy sweep", "Read every provisioned workspace for rules that change what done means."),
    (
        "P2",
        "resolve",
        "Enumerate and resolve",
        "Pick one target per system with cited evidence; lock every look-alike.",
    ),
    ("P3", "done", "Definition of done", "A typed checklist. Code adds the deliverables the policy requires."),
    ("P4", "plan", "Plan", "Only the writes done needs. The plan is dry-run through the mutation gate."),
    ("P5", "execute", "Gate, execute, read back", "Gate check, idempotent call, immediate read-back."),
    ("P6", "verify", "Verify", "Fresh end-state reads, cross-system check, protected-set audit."),
    ("P7", "deliver", "Deliver", "Unsent customer draft, owner review record, channel update, status from evidence."),
)


# --------------------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------------------


def load_receipt(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def load_trace(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(cast(dict[str, Any], json.loads(line)))
    return rows


def _map(value: object) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _list(value: object) -> list[Any]:
    return cast(list[Any], value) if isinstance(value, list) else []


def _records(value: object) -> list[dict[str, Any]]:
    return [cast(dict[str, Any], item) for item in _list(value) if isinstance(item, dict)]


def _str(value: object) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def e(value: object) -> str:
    return html.escape(_str(value), quote=True)


# --------------------------------------------------------------------------------------
# Derived views (pure functions over receipt + trace)
# --------------------------------------------------------------------------------------


def decode_draft(raw: str) -> dict[str, str]:
    """Decode a base64url RFC 2822 draft into to/subject/body. Empty strings when undecodable."""
    try:
        text = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return {"to": "", "subject": "", "body": ""}
    head, _, body = text.replace("\r\n", "\n").partition("\n\n")
    fields = {"to": "", "subject": "", "body": body.strip()}
    for line in head.split("\n"):
        key, _, val = line.partition(":")
        if key.strip().lower() in ("to", "subject"):
            fields[key.strip().lower()] = val.strip()
    return fields


def gate_by_sequence(receipt: Mapping[str, Any]) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for entry in _records(receipt.get("ledger")):
        gate = entry.get("gate")
        if isinstance(gate, dict):
            out[int(entry.get("sequence", 0))] = cast(dict[str, Any], gate)
    return out


def ticker_rows(receipt: Mapping[str, Any], trace: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """One row per provider call in the trace, plus one row per refusal (which never reached the network)."""
    gates = gate_by_sequence(receipt)
    rows: list[dict[str, str]] = []
    for item in trace:
        request = _map(item.get("request"))
        response = _map(item.get("response"))
        seq = int(item.get("sequence", 0))
        gate = gates.get(seq)
        if gate is None:
            decision, tone = "read", "muted"
        elif gate.get("allowed"):
            decision, tone = f"{_str(gate.get('rule'))} · {_str(gate.get('action_id'))}", "ok"
        else:
            decision, tone = f"refused · {_str(gate.get('rule'))}", "bad"
        rows.append(
            {
                "seq": str(seq),
                "phase": _str(item.get("phase")),
                "provider": _str(request.get("provider")),
                "method": _str(request.get("method")),
                "path": _str(request.get("path")),
                "status": _str(response.get("status_code")),
                "decision": decision,
                "tone": tone,
            }
        )
    for note in _list(receipt.get("notes")):
        text = _str(note)
        phase = text.split(":", 1)[0] if text[:1] == "P" and ":" in text[:4] else ""
        for verdict in _records(receipt.get("refusals")):
            action_id = _str(verdict.get("action_id"))
            if phase and f" {action_id} " in f"{text} ":
                refused = {
                    "seq": "—",
                    "phase": phase,
                    "provider": "",
                    "method": "",
                    "path": f"action {action_id}: never sent",
                    "status": "",
                    "decision": f"REFUSED · {_str(verdict.get('rule'))}",
                    "tone": "bad",
                }
                index = next((i for i, row in enumerate(rows) if row["phase"] > phase), len(rows))
                rows.insert(index, refused)
    return rows


def counts(receipt: Mapping[str, Any], trace: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    gates = gate_by_sequence(receipt)
    evidence = _records(receipt.get("evidence"))
    return {
        "calls": len(trace),
        "writes": sum(1 for gate in gates.values() if gate.get("allowed")),
        "refusals": len(_records(receipt.get("refusals"))),
        "checks": len(evidence),
        "matched": sum(1 for item in evidence if item.get("match")),
    }


# --------------------------------------------------------------------------------------
# HTML fragments
# --------------------------------------------------------------------------------------


def _chips(values: Iterable[object], tone: str = "") -> str:
    cls = f"chip chip-{tone}" if tone else "chip"
    return "".join(f'<span class="{cls}">{e(v)}</span>' for v in values) or '<span class="muted">none</span>'


def _kv(pairs: Iterable[tuple[str, str]]) -> str:
    rows = "".join(f"<dt>{e(k)}</dt><dd>{v}</dd>" for k, v in pairs)
    return f'<dl class="kv">{rows}</dl>'


def _calls_line(trace: Sequence[Mapping[str, Any]], phase: str) -> str:
    items = [t for t in trace if t.get("phase") == phase]
    if not items:
        return f'<p class="calls muted">No provider calls in {e(phase)}.</p>'
    providers = sorted({_str(_map(t.get("request")).get("provider")) for t in items})
    return (
        f'<p class="calls muted">{len(items)} provider call(s) in {e(phase)} '
        f"({e(', '.join(providers))}), listed in the call ticker.</p>"
    )


def _check_row(item: Mapping[str, Any]) -> str:
    ok = bool(item.get("match"))
    mark = (
        '<span class="mark mark-ok" aria-label="match">✓</span>'
        if ok
        else '<span class="mark mark-bad" aria-label="mismatch">✗</span>'
    )
    return (
        f'<li class="check">{mark}<div><code>{e(item.get("check"))}</code> '
        f'<span class="muted">{e(item.get("provider"))} · {e(item.get("resource"))}</span>'
        f'<div class="ev"><span class="ev-k">expected</span> <span class="ev-v">{e(item.get("expected"))}</span></div>'
        f'<div class="ev"><span class="ev-k">observed</span> <span class="ev-v">{e(item.get("observed"))}</span></div></div></li>'
    )


def _checks(items: Iterable[Mapping[str, Any]]) -> str:
    body = "".join(_check_row(i) for i in items)
    return f'<ul class="checks">{body}</ul>' if body else '<p class="muted">No checks.</p>'


def _evidence(receipt: Mapping[str, Any], prefix: str) -> list[dict[str, Any]]:
    return [i for i in _records(receipt.get("evidence")) if _str(i.get("check")).startswith(prefix)]


def panel_p0(receipt: Mapping[str, Any], trace: Sequence[Mapping[str, Any]]) -> str:
    frame = _map(_map(receipt.get("request")).get("frame"))
    return _kv(
        [
            ("reporter", e(frame.get("reporter"))),
            ("originating channel", f"<code>{e(frame.get('originating_channel'))}</code>"),
            ("role", e(frame.get("role"))),
            ("subject entities", _chips(_list(frame.get("subject_entities")), "accent")),
            ("requested change", e(frame.get("requested_change"))),
            ("prohibitions (verbatim)", _chips(_list(frame.get("explicit_prohibitions")), "bad")),
            ("distractor hint", e(frame.get("distractor_hint"))),
            ("providers", _chips(_list(_map(receipt.get("request")).get("providers")))),
        ]
    ) + _calls_line(trace, "P0")


def panel_p1(receipt: Mapping[str, Any], trace: Sequence[Mapping[str, Any]]) -> str:
    parts: list[str] = []
    for policy in _records(receipt.get("policies")):
        parts.append(
            '<figure class="policy">'
            f'<blockquote class="quote">{e(policy.get("quote"))}</blockquote>'
            f'<figcaption><span class="chip chip-warn">{e(policy.get("kind"))}</span> '
            f"found in <code>{e(policy.get('provider'))}:{e(policy.get('resource_ref'))}</code></figcaption></figure>"
        )
    if not parts:
        parts.append('<p class="muted">No policy found.</p>')
    return "".join(parts) + _calls_line(trace, "P1")


def panel_p2(receipt: Mapping[str, Any], trace: Sequence[Mapping[str, Any]]) -> str:
    targets = {(_str(t.get("provider")), _str(t.get("resource_id"))): t for t in _records(receipt.get("targets"))}
    protected = _map(receipt.get("protected"))
    locked = {_str(i) for i in _list(protected.get("ids"))}
    rows: list[str] = []
    for c in _records(receipt.get("candidates")):
        key = (_str(c.get("provider")), _str(c.get("resource_id")))
        target = targets.get(key)
        if target is not None:
            cls, verdict = "chosen", f'<span class="pill pill-ok">chosen · {e(target.get("confidence"))}</span>'
            why = _chips(_list(target.get("evidence")), "ok")
        elif key[1] in locked:
            cls, verdict, why = "protected", '<span class="pill pill-bad">locked look-alike</span>', ""
        else:
            cls, verdict, why = "", '<span class="pill">not chosen</span>', ""
        detail = " · ".join(_str(c.get(f)) for f in ("domain", "email", "lifecycle") if c.get(f))
        rows.append(
            f'<tr class="{cls}"><td><code>{e(c.get("provider"))}</code></td><td><code>{e(c.get("resource_id"))}</code></td>'
            f'<td>{e(c.get("name"))}<div class="sub">{e(detail)}</div></td><td>{verdict}<div class="sub">{why}</div></td></tr>'
        )
    table = (
        '<div class="scroll" tabindex="0" role="region" aria-label="Candidates"><table><thead><tr>'
        "<th>provider</th><th>id</th><th>record</th><th>decision and evidence</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )
    lock = _kv(
        [
            ("protected ids", _chips(_list(protected.get("ids")), "bad")),
            ("protected names", _chips(_list(protected.get("names")), "bad")),
            ("protected domains", _chips(_list(protected.get("domains")), "bad")),
            ("protected emails", _chips(_list(protected.get("emails")), "bad")),
        ]
    )
    return table + "<h3>Protected set, enforced by the gate</h3>" + lock + _calls_line(trace, "P2")


def panel_p3(receipt: Mapping[str, Any], trace: Sequence[Mapping[str, Any]]) -> str:
    dod = _map(receipt.get("definition_of_done"))
    end = "".join(
        f"<li><code>end_state[{i}]</code> <code>{e(s.get('provider'))} {e(s.get('resource'))}.{e(s.get('field'))}</code> "
        f"{e(s.get('comparison'))} <strong>{e(s.get('expected'))}</strong></li>"
        for i, s in enumerate(_records(dod.get("end_state")))
    )
    deliverables = "".join(
        f"<li><code>{e(d.get('kind'))}</code>"
        + (f' <span class="muted">via {e(d.get("provider"))}</span>' if d.get("provider") else "")
        + (f' <span class="chip chip-warn">because {e(d.get("because"))}</span>' if d.get("because") else "")
        + "</li>"
        for d in _records(dod.get("deliverables"))
    )
    facts = _map(dod.get("facts"))
    return (
        f'<p class="lede">{e(dod.get("summary"))}</p>'
        f'<h3>End state</h3><ul class="items">{end}</ul>'
        f'<h3>Deliverables</h3><ul class="items">{deliverables}</ul>'
        + _kv(
            [
                ("facts", _chips(f"{k}: {_str(v)}" for k, v in facts.items())),
                ("account owner", e(dod.get("account_owner"))),
                ("forbidden", _chips(_list(dod.get("forbidden")), "bad")),
            ]
        )
    )


def _action_row(action: Mapping[str, Any], refused: bool = False) -> str:
    fields = ", ".join(_str(f) for f in _list(action.get("fields")))
    satisfies = _chips(_list(action.get("satisfies")))
    cls = ' class="refused"' if refused else ""
    return (
        f"<tr{cls}><td><code>{e(action.get('id'))}</code></td><td>{e(action.get('kind'))}</td>"
        f"<td><code>{e(action.get('provider'))} {e(action.get('method'))} {e(action.get('path'))}</code>"
        f'<div class="sub">fields: {e(fields)}</div></td><td>{satisfies}</td></tr>'
    )


def refusal_cards(receipt: Mapping[str, Any]) -> str:
    notes = [_str(n) for n in _list(receipt.get("notes"))]
    cards: list[str] = []
    for verdict in _records(receipt.get("refusals")):
        action_id = _str(verdict.get("action_id"))
        note = next((n for n in notes if f" {action_id} " in f"{n} "), "")
        cards.append(
            f'<div class="refusal" role="group" aria-label="Refused write {e(action_id)}">'
            f'<div class="refusal-head"><span class="pill pill-bad">REFUSED</span> '
            f'<span>action <code class="big">{e(action_id)}</code></span> '
            f'<span>rule <code class="big">{e(verdict.get("rule"))}</code></span></div>'
            f'<p class="refusal-reason">{e(verdict.get("reason"))}</p>'
            + (f'<p class="sub">receipt note: <code>{e(note)}</code></p>' if note else "")
            + '<p class="sub">The model planned this write. The gate refused it in code; it never reached the network.</p></div>'
        )
    return "".join(cards)


def panel_p4(receipt: Mapping[str, Any], trace: Sequence[Mapping[str, Any]]) -> str:
    rows = "".join(_action_row(a) for a in _records(receipt.get("plan")))
    table = (
        '<div class="scroll" tabindex="0" role="region" aria-label="Planned writes"><table><thead><tr>'
        "<th>id</th><th>kind</th><th>request</th><th>satisfies</th></tr></thead>"
        f"<tbody>{rows}</tbody></table></div>"
    )
    refusals = refusal_cards(receipt)
    head = "<h3>Gate dry-run of the proposed plan</h3>" + (refusals or '<p class="muted">No refusals needed.</p>')
    return head + "<h3>Planned writes that passed</h3>" + table


def _gate_rows(receipt: Mapping[str, Any], phase: str) -> str:
    rows: list[str] = []
    for entry in _records(receipt.get("ledger")):
        gate = entry.get("gate")
        if entry.get("phase") != phase or not isinstance(gate, dict):
            continue
        g = cast(dict[str, Any], gate)
        tone = "ok" if g.get("allowed") else "bad"
        rows.append(
            f'<tr><td><code>{e(g.get("action_id"))}</code></td><td><span class="pill pill-{tone}">{e(g.get("rule"))}</span></td>'
            f"<td><code>{e(entry.get('provider'))} {e(entry.get('method'))} {e(entry.get('path'))}</code></td>"
            f"<td><code>#{e(entry.get('sequence'))}</code> · {e(entry.get('status_code'))}</td></tr>"
        )
    if not rows:
        return '<p class="muted">No gated writes.</p>'
    return (
        '<div class="scroll" tabindex="0" role="region" aria-label="Gate verdicts"><table><thead><tr>'
        "<th>action</th><th>gate</th><th>request</th><th>call · status</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


def panel_p5(receipt: Mapping[str, Any], trace: Sequence[Mapping[str, Any]]) -> str:
    n_refused = len(_records(receipt.get("refusals")))
    lead = (
        f'<p class="lede">{n_refused} write(s) refused before execution (P4). Every write that runs is gated again, then read back.</p>'
        if n_refused
        else ""
    )
    ids = [_str(a.get("id")) for a in _records(receipt.get("plan")) if a.get("kind") == "update"]
    readbacks = [
        i
        for i in _evidence(receipt, "readback:")
        if _str(i.get("check")).split(":")[1:2] and _str(i.get("check")).split(":")[1] in ids
    ]
    return (
        lead
        + "<h3>Gate verdicts</h3>"
        + _gate_rows(receipt, "P5")
        + "<h3>Read-back: expected vs observed</h3>"
        + _checks(readbacks)
    )


def panel_p6(receipt: Mapping[str, Any], trace: Sequence[Mapping[str, Any]]) -> str:
    items = [
        i
        for i in _records(receipt.get("evidence"))
        if _str(i.get("check")).startswith(("end_state", "cross_system", "protected_unchanged", "duplicate"))
    ]
    return _checks(items) + _calls_line(trace, "P6")


def panel_p7(receipt: Mapping[str, Any], trace: Sequence[Mapping[str, Any]]) -> str:
    plan = {_str(a.get("id")): a for a in _records(receipt.get("plan"))}
    created = _map(receipt.get("created"))
    parts: list[str] = []
    for action in plan.values():
        if action.get("kind") != "draft":
            continue
        raw = _str(_map(_map(action.get("body")).get("message")).get("raw"))
        draft = decode_draft(raw)
        labels: list[str] = []
        for item in trace:
            body = _map(_map(item.get("response")).get("body"))
            if _map(item.get("request")).get("method") == "POST" and body.get("id") == created.get(
                _str(action.get("id"))
            ):
                labels = [_str(x) for x in _list(_map(body.get("message")).get("labelIds"))]
        parts.append(
            '<article class="card"><header class="card-head"><h3>Customer confirmation</h3>'
            f'<span class="pill pill-warn">UNSENT</span>{_chips(labels, "warn")}</header>'
            + _kv([("to", f"<code>{e(draft['to'])}</code>"), ("subject", e(draft["subject"]))])
            + f'<pre class="msg">{e(draft["body"])}</pre>'
            f'<p class="sub">draft <code>{e(created.get(_str(action.get("id"))))}</code>, awaiting account-owner review. Sending is not a plannable kind.</p></article>'
        )
    titles = {
        "deliverable:owner_review_record": "Owner review record",
        "deliverable:originating_channel_update": "Channel update",
    }
    for action in plan.values():
        if action.get("kind") != "message":
            continue
        aid = _str(action.get("id"))
        body = _map(action.get("body"))
        title = next((titles[_str(s)] for s in _list(action.get("satisfies")) if _str(s) in titles), "Message")
        parts.append(
            f'<article class="card"><header class="card-head"><h3>{e(title)}</h3>'
            f'<span class="chip">{e(action.get("provider"))} <code>{e(body.get("channel"))}</code> ts {e(created.get(aid))}</span></header>'
            f'<pre class="msg">{e(body.get("text"))}</pre></article>'
        )
    gates = _gate_rows(receipt, "P7")
    deliverable_checks = _checks(_evidence(receipt, "deliverable:"))
    message_readbacks = [
        i
        for i in _evidence(receipt, "readback:")
        if _str(i.get("check")).split(":")[1:2]
        and plan.get(_str(i.get("check")).split(":")[1], {}).get("kind") in ("message", "draft")
    ]
    c = counts(receipt, trace)
    status = _str(receipt.get("status"))
    tone = {"completed": "ok", "partial": "warn", "escalated": "accent"}.get(status, "bad")
    final = (
        f'<div class="final"><span class="k">final status</span> <span class="pill pill-{tone} pill-lg">{e(status)}</span>'
        f'<span class="muted">computed from {c["matched"]}/{c["checks"]} evidence checks matching provider state</span></div>'
    )
    return (
        final
        + "".join(parts)
        + "<h3>Gate verdicts</h3>"
        + gates
        + "<h3>Message read-backs</h3>"
        + _checks(message_readbacks)
        + "<h3>Deliverable checks</h3>"
        + deliverable_checks
    )


PANELS = (panel_p0, panel_p1, panel_p2, panel_p3, panel_p4, panel_p5, panel_p6, panel_p7)


# --------------------------------------------------------------------------------------
# Page
# --------------------------------------------------------------------------------------

CSS = """
:root{--ground:#FAFAF7;--panel:#FFFFFF;--line:#D9DCE1;--text:#0B0D10;--muted:#4F5864;--ok:#116329;--ok-bg:#E6F4EA;
--bad:#A40E26;--bad-bg:#FDECEC;--warn:#7A4A00;--warn-bg:#FFF4D6;--accent:#0550AE;--accent-bg:#E7F0FC;--focus:#0550AE;
--sans:ui-sans-serif,-apple-system,"Segoe UI",Inter,system-ui,sans-serif;--mono:ui-monospace,"JetBrains Mono",SFMono-Regular,Menlo,monospace;color-scheme:light}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--ground:#0B0D10;--panel:#12161B;--line:#262E39;--text:#E6EAF0;
--muted:#9AA5B4;--ok:#56D364;--ok-bg:#0F2417;--bad:#FF7B72;--bad-bg:#2A1212;--warn:#E3B341;--warn-bg:#271E08;--accent:#79B8FF;
--accent-bg:#0E1D30;--focus:#79B8FF;color-scheme:dark}}
:root[data-theme="dark"]{--ground:#0B0D10;--panel:#12161B;--line:#262E39;--text:#E6EAF0;--muted:#9AA5B4;--ok:#56D364;--ok-bg:#0F2417;
--bad:#FF7B72;--bad-bg:#2A1212;--warn:#E3B341;--warn-bg:#271E08;--accent:#79B8FF;--accent-bg:#0E1D30;--focus:#79B8FF;color-scheme:dark}
*{box-sizing:border-box}
html{background:var(--ground)}
body{margin:0;background:var(--ground);color:var(--text);font:15px/1.55 var(--sans);-webkit-font-smoothing:antialiased}
a{color:var(--accent)}
:focus-visible{outline:3px solid var(--focus);outline-offset:2px;border-radius:4px}
code,pre,.mono{font-family:var(--mono);font-size:13px}
.wrap{max-width:1180px;margin:0 auto;padding:0 20px}
.skip{position:absolute;left:-999px;top:8px;background:var(--panel);color:var(--text);padding:8px 12px;border:1px solid var(--line)}
.skip:focus{left:8px;z-index:20}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
header.hero{padding:28px 0 18px;border-bottom:1px solid var(--line)}
.brand{display:flex;align-items:center;gap:12px;flex-wrap:wrap;justify-content:space-between}
.brand b{font-size:20px;letter-spacing:-.01em}
.brand .tag{color:var(--muted);font-size:14px}
h1{font-size:clamp(22px,3.2vw,32px);line-height:1.2;letter-spacing:-.02em;margin:18px 0 8px;max-width:30ch}
.promise{font-size:clamp(17px,2vw,20px);font-weight:600;color:var(--accent);margin:0 0 14px}
.request{margin:0 0 14px;padding:12px 16px;border-left:3px solid var(--line);background:var(--panel);white-space:pre-wrap;max-width:88ch}
.request .k{display:block;color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.08em;margin-bottom:4px}
.install{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.install code{display:inline-block;padding:8px 12px;background:var(--panel);border:1px solid var(--line);border-radius:6px;font-size:14px;overflow-x:auto;max-width:100%}
.substrate{color:var(--muted);font-size:13px;margin:10px 0 0}
.stats{display:flex;gap:18px;flex-wrap:wrap;margin:14px 0 0;padding:0;list-style:none}
.stats li{display:flex;align-items:baseline;gap:6px}
.stats .n{font:700 22px/1 var(--mono)}
.stats .k{color:var(--muted);font-size:13px}
.stats .bad .n{color:var(--bad)} .stats .ok .n{color:var(--ok)}
.controls{position:sticky;top:0;z-index:10;background:var(--ground);border-bottom:1px solid var(--line);padding:10px 0}
.rail{display:grid;grid-template-columns:repeat(8,minmax(0,1fr));gap:6px;margin:0;padding:0;list-style:none}
.rail button{width:100%;font:600 12px/1.2 var(--mono);text-align:left;padding:8px;border-radius:6px;border:1px solid var(--line);
background:var(--panel);color:var(--muted);cursor:pointer;min-height:44px}
.rail button .pid{display:block;color:var(--text)}
.rail button.done{border-color:var(--ok);color:var(--ok)}
.rail button.alert{border-color:var(--bad)}
.rail button[aria-current="step"]{background:var(--text);color:var(--ground);border-color:var(--text)}
.rail button[aria-current="step"] .pid{color:var(--ground)}
.buttons{display:flex;gap:8px;align-items:center;margin-top:8px;flex-wrap:wrap}
.btn{font:600 13px/1 var(--sans);padding:0 14px;height:36px;border-radius:6px;border:1px solid var(--line);background:var(--panel);color:var(--text);cursor:pointer}
.btn-primary{background:var(--text);color:var(--ground);border-color:var(--text)}
.hint{color:var(--muted);font-size:12px;margin-left:auto}
.stage{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,380px);gap:20px;padding:20px 0 28px;align-items:start}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:20px;margin:0 0 16px;scroll-margin-top:150px}
.js .panel{display:none}
.js .panel.current{display:block}
.panel h2{margin:0 0 4px;font-size:20px;line-height:1.3;display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}
.panel h2 .pid{font:700 14px/1 var(--mono);color:var(--accent)}
.panel .owns{color:var(--muted);margin:0 0 16px}
h3{margin:18px 0 8px;font-size:12px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.08em}
.lede{margin:0 0 12px}
.muted{color:var(--muted)}
.sub{color:var(--muted);font-size:13px;margin-top:4px}
.calls{font-size:13px;margin:14px 0 0}
.kv{display:grid;grid-template-columns:minmax(120px,190px) 1fr;gap:6px 18px;margin:0}
.kv dt{color:var(--muted);font-size:13px;padding-top:2px}
.kv dd{margin:0;min-width:0;overflow-wrap:anywhere}
.chip{display:inline-block;padding:1px 8px;border-radius:999px;border:1px solid var(--line);font-size:12.5px;line-height:18px;margin:2px 4px 2px 0;overflow-wrap:anywhere}
.chip-ok{color:var(--ok);background:var(--ok-bg);border-color:var(--ok)}
.chip-bad{color:var(--bad);background:var(--bad-bg);border-color:var(--bad)}
.chip-warn{color:var(--warn);background:var(--warn-bg);border-color:var(--warn)}
.chip-accent{color:var(--accent);background:var(--accent-bg);border-color:var(--accent)}
.pill{display:inline-block;padding:2px 10px;border-radius:999px;font-size:12px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;border:1px solid var(--line);white-space:nowrap}
.pill-ok{color:var(--ok);background:var(--ok-bg);border-color:var(--ok)}
.pill-bad{color:var(--bad);background:var(--bad-bg);border-color:var(--bad)}
.pill-warn{color:var(--warn);background:var(--warn-bg);border-color:var(--warn)}
.pill-accent{color:var(--accent);background:var(--accent-bg);border-color:var(--accent)}
.pill-lg{font-size:15px;padding:4px 14px}
.policy{margin:0 0 12px}
.quote{margin:0 0 6px;padding:12px 16px;border-left:4px solid var(--warn);background:var(--warn-bg);font-size:16px}
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:6px}
table{width:100%;border-collapse:collapse;font-size:14px}
th{text-align:left;color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.05em;padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:0}
td:first-child{border-left:4px solid transparent}
tr.chosen td:first-child{border-left-color:var(--ok)}
tr.protected td:first-child,tr.refused td:first-child{border-left-color:var(--bad)}
tr.protected td{background:var(--bad-bg)}
.items{margin:0 0 8px;padding-left:18px}
.items li{margin:4px 0;overflow-wrap:anywhere}
.refusal{border:2px solid var(--bad);background:var(--bad-bg);border-radius:8px;padding:14px 16px;margin:0 0 12px}
.refusal-head{display:flex;gap:14px;align-items:center;flex-wrap:wrap;color:var(--bad);font-weight:600}
.refusal code.big{font-size:16px;font-weight:700;color:var(--bad)}
.refusal-reason{font-size:17px;font-weight:600;margin:8px 0 4px;color:var(--text)}
.checks{margin:0;padding:0;list-style:none}
.check{display:grid;grid-template-columns:24px minmax(0,1fr);gap:0 8px;padding:8px 0;border-top:1px solid var(--line)}
.check:first-child{border-top:0}
.mark{font-weight:700;font-size:16px;line-height:22px}
.mark-ok{color:var(--ok)} .mark-bad{color:var(--bad)}
.ev{display:grid;grid-template-columns:72px minmax(0,1fr);gap:8px;font-size:13.5px;margin-top:2px}
.ev-k{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.05em;padding-top:1px}
.ev-v{font-family:var(--mono);font-size:13px;overflow-wrap:anywhere;white-space:pre-wrap}
.card{border:1px solid var(--line);border-radius:8px;padding:14px 16px;margin:0 0 12px}
.card-head{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px}
.card-head h3{margin:0 8px 0 0;color:var(--text);font-size:14px;text-transform:none;letter-spacing:0}
.msg{margin:8px 0 0;padding:12px;background:var(--ground);border:1px solid var(--line);border-radius:6px;white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px;line-height:1.5}
.final{display:flex;gap:12px;align-items:center;flex-wrap:wrap;padding:12px 16px;border:2px solid var(--ok);border-radius:8px;margin:0 0 14px}
.final .k{font-weight:700}
aside.ticker{position:sticky;top:160px}
aside.ticker h2{font-size:14px;margin:0 0 8px}
.ticker .scroll{max-height:62vh;overflow:auto;background:var(--panel)}
.ticker table{font-size:12px}
.ticker td,.ticker th{padding:5px 8px;white-space:nowrap}
.ticker td.path{font-family:var(--mono);max-width:180px;overflow:hidden;text-overflow:ellipsis}
.ticker tr.future{opacity:.35}
.ticker tr.now td{background:var(--accent-bg)}
.ticker tr.tone-bad td{color:var(--bad);font-weight:700;background:var(--bad-bg)}
.d-ok{color:var(--ok)} .d-muted{color:var(--muted)} .d-bad{color:var(--bad)}
footer{border-top:1px solid var(--line);padding:18px 0 32px;color:var(--muted);font-size:13px}
footer nav ul{display:flex;gap:18px;flex-wrap:wrap;list-style:none;margin:0 0 8px;padding:0}
@media (max-width:900px){.stage{grid-template-columns:minmax(0,1fr)}aside.ticker{position:static}.rail{grid-template-columns:repeat(4,minmax(0,1fr))}.hint{display:none}}
@media (max-width:480px){.wrap{padding:0 14px}.panel{padding:14px}.kv{grid-template-columns:1fr;gap:0}.kv dd{margin-bottom:8px}.rail button{font-size:11px;padding:6px}}
@media (prefers-reduced-motion:no-preference){.panel.current{animation:in .25s ease-out}@keyframes in{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}}
"""

JS = """
(function(){
  var root=document.documentElement; root.classList.add('js');
  var panels=[].slice.call(document.querySelectorAll('.panel'));
  var tabs=[].slice.call(document.querySelectorAll('.rail button'));
  var rows=[].slice.call(document.querySelectorAll('.ticker tbody tr'));
  var live=document.getElementById('live');
  var playBtn=document.getElementById('play');
  var i=0, timer=null, N=panels.length;
  var order=tabs.map(function(t){return t.getAttribute('data-phase');});
  function show(n, announce){
    i=Math.max(0,Math.min(N-1,n));
    panels.forEach(function(p,k){p.classList.toggle('current',k===i);});
    tabs.forEach(function(t,k){ if(k===i){t.setAttribute('aria-current','step');} else {t.removeAttribute('aria-current');} t.classList.toggle('done',k<i); });
    var cur=order[i];
    rows.forEach(function(r){var k=order.indexOf(r.getAttribute('data-phase')); r.classList.toggle('future',k>i); r.classList.toggle('now',k===i);});
    var firstNow=document.querySelector('.ticker tr.now');
    if(firstNow){var box=firstNow.closest('.scroll'); if(box){box.scrollTop=firstNow.offsetTop-box.querySelector('thead').offsetHeight;}}
    if(announce!==false){live.textContent='Step '+(i+1)+' of '+N+': '+panels[i].getAttribute('data-title');}
    try{history.replaceState(null,'','#'+cur.toLowerCase());}catch(err){}
  }
  function stop(){ if(timer){clearInterval(timer);timer=null;} playBtn.textContent='Play'; playBtn.setAttribute('aria-pressed','false'); }
  function play(){ if(i>=N-1){show(0);} playBtn.textContent='Pause'; playBtn.setAttribute('aria-pressed','true');
    timer=setInterval(function(){ if(i>=N-1){stop();return;} show(i+1); },2600); }
  playBtn.addEventListener('click',function(){ timer?stop():play(); });
  document.getElementById('step').addEventListener('click',function(){ stop(); show(i+1); });
  document.getElementById('back').addEventListener('click',function(){ stop(); show(i-1); });
  document.getElementById('reset').addEventListener('click',function(){ stop(); show(0); });
  tabs.forEach(function(t,k){ t.addEventListener('click',function(){ stop(); show(k); }); });
  document.addEventListener('keydown',function(ev){
    if(ev.altKey||ev.ctrlKey||ev.metaKey){return;}
    var tag=(ev.target&&ev.target.tagName)||'';
    if(ev.key==='ArrowRight'){stop();show(i+1);ev.preventDefault();}
    else if(ev.key==='ArrowLeft'){stop();show(i-1);ev.preventDefault();}
    else if(ev.key===' '&&tag!=='BUTTON'&&tag!=='A'&&tag!=='INPUT'){ev.preventDefault(); timer?stop():play();}
  });
  var themeBtn=document.getElementById('theme');
  themeBtn.addEventListener('click',function(){
    var dark=root.getAttribute('data-theme')?root.getAttribute('data-theme')==='dark':window.matchMedia('(prefers-color-scheme: dark)').matches;
    root.setAttribute('data-theme',dark?'light':'dark'); themeBtn.setAttribute('aria-pressed',String(!dark));
  });
  var m=/^#p([0-7])$/.exec(location.hash||'');
  var reduced=window.matchMedia&&window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if(m){show(+m[1]);} else {show(0,false); if(!reduced){play();}}
})();
"""


def render_page(receipt: Mapping[str, Any], trace: Sequence[Mapping[str, Any]]) -> str:
    request = _map(receipt.get("request"))
    prompt = _str(request.get("prompt"))
    headline = prompt.split("\n", 1)[0]
    c = counts(receipt, trace)
    refused_phases = {row["phase"] for row in ticker_rows(receipt, trace) if row["tone"] == "bad"}

    rail = "".join(
        f'<li><button type="button" data-phase="{pid}"{" class=alert" if pid in refused_phases else ""} aria-controls="panel-{pid.lower()}">'
        f'<span class="pid">{pid}</span>{e(label)}</button></li>'
        for pid, label, _, _ in PHASES
    )
    panels = "".join(
        f'<section class="panel" id="panel-{pid.lower()}" data-title="{pid} {e(title)}" aria-labelledby="h-{pid.lower()}">'
        f'<h2 id="h-{pid.lower()}"><span class="pid">{pid}</span>{e(title)}</h2><p class="owns">{e(owns)}</p>'
        f"{render(receipt, trace)}</section>"
        for (pid, _, title, owns), render in zip(PHASES, PANELS, strict=True)
    )
    ticker = "".join(
        f'<tr data-phase="{e(r["phase"])}" class="tone-{e(r["tone"])}"><td>{e(r["seq"])}</td><td>{e(r["phase"])}</td>'
        f'<td>{e(r["provider"])}</td><td>{e(r["method"])}</td><td class="path" title="{e(r["path"])}">{e(r["path"])}</td>'
        f'<td class="d-{e(r["tone"])}">{e(r["decision"])}</td></tr>'
        for r in ticker_rows(receipt, trace)
    )
    links = "".join(f'<li><a href="{e(url)}">{e(label)}</a></li>' for label, url in LINKS)
    meta = _map(receipt.get("meta"))
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Benchpress replay: one real run, step by step</title>
<meta name="description" content="{e(PROMISE)} A step-through replay of a real Benchpress run rendered from its receipt and trace.">
<link rel="icon" href="data:,">
<style>{CSS}</style>
</head>
<body>
<a class="skip" href="#stage">Skip to the replay</a>
<header class="hero" role="banner"><div class="wrap">
<div class="brand"><div><b>Benchpress</b> <span class="tag">the reliability layer for AI agents with write access</span></div>
<button class="btn" id="theme" type="button" aria-pressed="false">Toggle theme</button></div>
<h1>{e(headline)}</h1>
<p class="promise">{e(PROMISE)}</p>
<details class="request"><summary><span class="k">Full request (receipt.request.prompt)</span></summary>{e(prompt)}</details>
<div class="install"><span class="muted">Run it yourself:</span> <code>{e(INSTALL)}</code></div>
<p class="substrate"><strong>Substrate:</strong> {e(SUBSTRATE)} Trial <code>{e(receipt.get("trial_id"))}</code>, model <code>{e(meta.get("model"))}</code>.</p>
<ul class="stats" aria-label="Run totals">
<li><span class="n">{c["calls"]}</span><span class="k">provider calls</span></li>
<li class="ok"><span class="n">{c["writes"]}</span><span class="k">gated writes</span></li>
<li class="bad"><span class="n">{c["refusals"]}</span><span class="k">refused</span></li>
<li class="ok"><span class="n">{c["matched"]}/{c["checks"]}</span><span class="k">evidence checks match</span></li>
<li><span class="n">{e(receipt.get("status"))}</span><span class="k">status</span></li>
</ul>
</div></header>
<nav class="controls" aria-label="Replay controls"><div class="wrap">
<ol class="rail">{rail}</ol>
<div class="buttons">
<button class="btn btn-primary" id="play" type="button" aria-pressed="false">Play</button>
<button class="btn" id="back" type="button">Back</button>
<button class="btn" id="step" type="button">Step</button>
<button class="btn" id="reset" type="button">Reset</button>
<span class="hint">Keys: ← → step · space play/pause</span>
</div>
<p id="live" class="sr" aria-live="polite" aria-atomic="true"></p>
</div></nav>
<main id="stage" class="wrap stage">
<div class="panels">{panels}</div>
<aside class="ticker" aria-label="Tool-call ticker">
<h2>Call ticker <span class="muted">(benchpress-trace.jsonl)</span></h2>
<div class="scroll" tabindex="0" role="region" aria-label="Provider calls in order">
<table><thead><tr><th>#</th><th>phase</th><th>provider</th><th>method</th><th>path</th><th>gate</th></tr></thead>
<tbody>{ticker}</tbody></table></div>
<p class="sub">{c["calls"]} calls · {c["writes"]} writes allowed · {c["refusals"]} refused before the network</p>
</aside>
</main>
<footer role="contentinfo"><div class="wrap">
<nav aria-label="Project links"><ul>{links}</ul></nav>
<p>Rendered by <code>scripts/render_replay_page.py</code> from <code>receipt.json</code> and <code>benchpress-trace.jsonl</code> of <code>benchpress demo</code>. Every value above comes from those files.</p>
</div></footer>
<script>{JS}</script>
</body>
</html>
"""


def write_page(run_dir: Path, out: Path) -> Path:
    receipt = load_receipt(run_dir / "receipt.json")
    trace = load_trace(run_dir / "benchpress-trace.jsonl")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_page(receipt, trace), encoding="utf-8")
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--run-dir", type=Path, default=Path("runs/page-demo"), help="directory holding receipt.json + trace"
    )
    parser.add_argument("--out", type=Path, default=Path("docs/demo/index.html"))
    args = parser.parse_args(argv)
    path = write_page(cast(Path, args.run_dir), cast(Path, args.out))
    print(f"wrote {path} ({path.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
