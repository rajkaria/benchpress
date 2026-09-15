"""Generate the gateway parity fixture: 100 deterministic writes over three sessions, covering every outcome.

    uv run python scripts/gen_gateway_parity.py

Writes `tests/fixtures/parity/sessions.json` (the workspace, the fixed clock and each session's context) and
`tests/fixtures/parity/calls.jsonl` (one call per line: `n`, the session, the action, the provider's scripted write
and read-back responses, and the status every mode must report). `tests/test_parity_gateway.py` runs the calls
through the library, the HTTP gateway and the MCP gateway, requires byte-identical `benchpress-write/1` lines, and
checks that this script still builds exactly what is committed. Nothing here is random.

Every name is invented: Rivermill Studio (`rivermill.example`) in HubSpot, Harlow Bakery (`harlowbakery.example`) in
Stripe, `example-org` on GitHub. A call that is not a replay targets its own record, `9000 + n`, except a protected
write, which targets the session's protected record. A replay repeats an earlier verified call of its session
exactly. The write script of every call the gate must refuse is a tripwire (`{"raise": ...}`): if the gate ever let
one through, the provider would raise and the call would report `failed` instead of `refused`.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any, NamedTuple, TypeVar

T = TypeVar("T")
ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "parity"
WORKSPACE = "local"
CLOCK = "2026-09-14T00:00:00.000Z"
CRM, BILLING, ISSUES = "p-crm", "p-billing", "p-issues"
REPO = "/repos/example-org/widgets"
TRIPWIRE = "the gate must refuse this write before it reaches the provider"

SESSIONS: dict[str, dict[str, Any]] = {
    CRM: {
        "user_prompt": (
            "Rivermill Studio (rivermill.example) asked us to keep its HubSpot company records current: renewal "
            "notes, lifecycle stages and billing contacts at rivermill.example addresses. Company 702 is a duplicate "
            "prospect record and must not change."
        ),
        "providers": ["hubspot"],
        "protected": {"ids": ["702"]},
        "write_scope": ["hubspot"],
        "forbidden": ["send_email", "delete_any"],
    },
    BILLING: {
        "user_prompt": (
            "Harlow Bakery (harlowbakery.example) asked us to update its Stripe customers: plan tier and renewal "
            "month metadata, descriptions, and billing emails at harlowbakery.example addresses. Customer cus_P2 is "
            "a closed duplicate and must not change."
        ),
        "providers": ["stripe"],
        "protected": {"ids": ["cus_P2"]},
        "write_scope": ["stripe"],
        "forbidden": ["create_charge", "create_invoice", "update_subscription", "delete_any"],
    },
    ISSUES: {
        "user_prompt": (
            "Triage the example-org/widgets issue queue: post status comments on the queued issues and add the "
            "triage label where asked. Never close an issue or push code."
        ),
        "providers": ["github"],
        "write_scope": ["github"],
        "forbidden": ["close_regression", "merge_pr", "push_commit", "delete_any"],
    },
}

EXPECT: dict[str, str] = {
    "verified": "verified",
    "mismatch_value": "mismatch",
    "mismatch_missing": "mismatch",
    "protected": "refused",
    "external": "refused",
    "method": "refused",
    "control_plane": "refused",
    "replay": "refused",
    "failed": "failed",
    "readback_404": "unverified",
    "no_readback": "unverified",
}
MIX = Counter({"verified": 40, "mismatch": 20, "refused": 30, "failed": 5, "unverified": 5})

# (field, value template, the stale value a contradicting read-back shows). No value contains another, so a stale
# read-back can never pass `values_match`'s containment check.
HUBSPOT_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("description", "Renewal notes for record {record}: invoices go to accounts payable",
     "Awaiting review by the account owner"),
    ("billing_contact_email", "ap+{record}@rivermill.example", "old-contact@rivermill.example"),
    ("lifecyclestage", "customer", "opportunity"),
    ("renewal_month", "October", "January"),
)  # fmt: skip
STRIPE_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("metadata[plan_tier]", "wholesale", "retail"),
    ("email", "billing+{record}@harlowbakery.example", "orders@harlowbakery.example"),
    ("metadata[renewal_month]", "March", "September"),
    ("description", "Harlow Bakery storefront account {record}", "Pending account setup"),
)
COMMENTS: tuple[tuple[str, str], ...] = (
    ("Status update for issue {record}: the fix is queued for the next release",
     "This comment was edited to remove its details"),
    ("Reproduced issue {record} on the current build and assigned it to the widgets team", "Could not reproduce"),
    ("Issue {record} needs a failing test before it can be reviewed", "Closing as a duplicate"),
)  # fmt: skip
# Each session's own customer is internal to it; the other customer's domain is external.
CRM_EXTERNAL = (
    ("description", "Send renewal notices for record {record} to ap@harlowbakery.example"),
    ("billing_contact_email", "ap+{record}@harlowbakery.example"),
)
BILLING_EXTERNAL = (
    ("email", "billing+{record}@rivermill.example"),
    ("metadata[invoice_cc]", "ap+{record}@rivermill.example"),
)
ISSUES_EXTERNAL = (
    "Forwarded issue {record} to ap@rivermill.example for a customer reply",
    "Copied the report on issue {record} to orders@harlowbakery.example",
)


class Row(NamedTuple):
    """`size` calls of one case in a session, dealt from round `start`. A replay row names the verified calls it
    repeats, by their order within the session's verified row.

    A `NamedTuple`, not a dataclass: the parity test loads this script with `importlib` without registering it in
    `sys.modules`, where a dataclass under postponed annotations cannot resolve its own module."""

    case: str
    size: int
    start: int
    replays: tuple[int, ...] = ()


# Within a round, rows deal in this order, so a replay always follows the verified call it repeats.
PLANS: dict[str, tuple[Row, ...]] = {
    CRM: (
        Row("verified", 14, 0), Row("mismatch_value", 4, 1), Row("protected", 4, 0), Row("mismatch_missing", 3, 2),
        Row("external", 2, 3), Row("failed", 2, 4), Row("method", 1, 5), Row("replay", 4, 6, (0, 2, 4, 6)),
        Row("control_plane", 1, 7), Row("readback_404", 1, 8), Row("no_readback", 1, 10),
    ),
    BILLING: (
        Row("verified", 13, 0), Row("mismatch_value", 4, 1), Row("protected", 4, 0), Row("mismatch_missing", 3, 2),
        Row("external", 2, 3), Row("failed", 2, 4), Row("method", 1, 5), Row("replay", 3, 6, (1, 3, 5)),
        Row("control_plane", 1, 7), Row("readback_404", 1, 9),
    ),
    ISSUES: (
        Row("verified", 13, 0), Row("mismatch_value", 4, 1), Row("mismatch_missing", 2, 2), Row("external", 2, 3),
        Row("failed", 1, 4), Row("method", 1, 5), Row("replay", 3, 6, (0, 3, 6)), Row("control_plane", 1, 7),
        Row("readback_404", 1, 8), Row("no_readback", 1, 10),
    ),
}  # fmt: skip

Script = dict[str, Any]
Built = tuple[dict[str, Any], Script, Script | None]


class Slot(NamedTuple):
    session: str
    case: str
    ordinal: int


def _deal(rows: Sequence[tuple[int, Sequence[T]]]) -> list[T]:
    """Round-robin: in round r, every row whose items span r contributes its item r - start, in row order."""
    rounds = max(start + len(items) for start, items in rows)
    return [items[r - start] for r in range(rounds) for start, items in rows if start <= r < start + len(items)]


def _ok(body: object, status_code: int = 200) -> Script:
    return {"status_code": status_code, "body": body}


def _tripwire() -> Script:
    return {"raise": TRIPWIRE}


# ---- HubSpot (p-crm) ------------------------------------------------------------------------------------------


def _hubspot_update(action_id: str, record: str, field: str, value: str, *, readback: bool = True) -> dict[str, Any]:
    path = f"/crm/v3/objects/companies/{record}"
    action: dict[str, Any] = {
        "id": action_id,
        "kind": "update",
        "provider": "hubspot",
        "method": "PATCH",
        "path": path,
        "body": {"properties": {field: value}},
        "fields": [field],
        "target_refs": [f"company:{record}"],
    }
    if readback:
        action["readback"] = {"path": path, "query": {"properties": field}, "field_path": f"properties.{field}"}
    return action


def _hubspot_company(record: str, properties: dict[str, str]) -> Script:
    return _ok({"id": record, "properties": properties})


def _crm(case: str, index: int, n: int) -> Built:
    record = str(9000 + n)
    action_id = f"rivermill-{record}"
    field, template, stale = HUBSPOT_FIELDS[index % len(HUBSPOT_FIELDS)]
    value = template.format(record=record)
    written = _hubspot_company(record, {field: value})
    if case == "verified":
        return _hubspot_update(action_id, record, field, value), written, _hubspot_company(record, {field: value})
    if case == "mismatch_value":
        return _hubspot_update(action_id, record, field, value), written, _hubspot_company(record, {field: stale})
    if case == "mismatch_missing":
        read = _hubspot_company(record, {"name": "Rivermill Studio"})
        return _hubspot_update(action_id, record, field, value), written, read
    if case == "protected":
        return _hubspot_update(action_id, "702", field, value), _tripwire(), None
    if case == "external":
        external_field, external = CRM_EXTERNAL[index]
        return _hubspot_update(action_id, record, external_field, external.format(record=record)), _tripwire(), None
    if case == "method":
        action = {
            "id": action_id,
            "kind": "update",
            "provider": "hubspot",
            "method": "DELETE",
            "path": f"/crm/v3/objects/companies/{record}",
            "target_refs": [f"company:{record}"],
        }
        return action, _tripwire(), None
    if case == "control_plane":
        action = _hubspot_update(action_id, record, field, value, readback=False)
        return {**action, "path": f"/admin/crm/v3/objects/companies/{record}"}, _tripwire(), None
    if case == "failed":
        return _hubspot_update(action_id, record, field, value), _ok({"message": "internal error"}, 500), None
    if case == "readback_404":
        return _hubspot_update(action_id, record, field, value), written, None
    if case == "no_readback":
        return _hubspot_update(action_id, record, field, value, readback=False), written, None
    raise ValueError(f"no {case!r} case for {CRM}")


# ---- Stripe (p-billing) ---------------------------------------------------------------------------------------


def _stripe_update(action_id: str, customer: str, field: str, value: str) -> dict[str, Any]:
    path = f"/v1/customers/{customer}"
    head, _, rest = field.partition("[")
    return {
        "id": action_id,
        "kind": "update",
        "provider": "stripe",
        "method": "POST",
        "path": path,
        "body": {field: value},
        "body_encoding": "form",
        "headers": {"Idempotency-Key": f"bp-{action_id}"},
        "fields": [field],
        "target_refs": [f"customer:{customer}"],
        "readback": {"path": path, "field_path": f"{head}.{rest.rstrip(']')}" if rest else field},
    }


def _stripe_customer(customer: str, field: str | None = None, value: str = "") -> Script:
    """A customer object; `field` (form-style, `metadata[plan_tier]`) sits nested where Stripe returns it."""
    body: dict[str, Any] = {"id": customer, "object": "customer", "metadata": {}}
    if field is not None:
        head, _, rest = field.partition("[")
        if rest:
            body[head] = {rest.rstrip("]"): value}
        else:
            body[field] = value
    return _ok(body)


def _billing(case: str, index: int, n: int) -> Built:
    record = str(9000 + n)
    customer = f"cus_{record}"
    action_id = f"harlow-{record}"
    field, template, stale = STRIPE_FIELDS[index % len(STRIPE_FIELDS)]
    value = template.format(record=record)
    written = _stripe_customer(customer, field, value)
    if case == "verified":
        return _stripe_update(action_id, customer, field, value), written, _stripe_customer(customer, field, value)
    if case == "mismatch_value":
        return _stripe_update(action_id, customer, field, value), written, _stripe_customer(customer, field, stale)
    if case == "mismatch_missing":
        return _stripe_update(action_id, customer, field, value), written, _stripe_customer(customer)
    if case == "protected":
        return _stripe_update(action_id, "cus_P2", field, value), _tripwire(), None
    if case == "external":
        external_field, external = BILLING_EXTERNAL[index]
        return _stripe_update(action_id, customer, external_field, external.format(record=record)), _tripwire(), None
    if case == "method":
        action = {
            "id": action_id,
            "kind": "update",
            "provider": "stripe",
            "method": "DELETE",
            "path": f"/v1/customers/{customer}",
            "target_refs": [f"customer:{customer}"],
        }
        return action, _tripwire(), None
    if case == "control_plane":
        action = {**_stripe_update(action_id, customer, field, value), "path": f"/reset/v1/customers/{customer}"}
        del action["readback"]
        return action, _tripwire(), None
    if case == "failed":
        failure = _ok({"error": {"type": "api_error", "message": "internal error"}}, 500)
        return _stripe_update(action_id, customer, field, value), failure, None
    if case == "readback_404":
        return _stripe_update(action_id, customer, field, value), written, None
    raise ValueError(f"no {case!r} case for {BILLING}")


# ---- GitHub (p-issues) ----------------------------------------------------------------------------------------


def _comment(action_id: str, issue: str, text: str) -> dict[str, Any]:
    return {
        "id": action_id,
        "kind": "message",
        "provider": "github",
        "method": "POST",
        "path": f"{REPO}/issues/{issue}/comments",
        "body": {"body": text},
        "fields": ["body"],
        "target_refs": [f"issue:example-org/widgets#{issue}"],
        "readback": {"path": f"{REPO}/issues/comments/{{created_id}}", "field_path": "body"},
    }


def _labels(action_id: str, issue: str, *, readback: bool = True) -> dict[str, Any]:
    action: dict[str, Any] = {
        "id": action_id,
        "kind": "update",
        "provider": "github",
        "method": "POST",
        "path": f"{REPO}/issues/{issue}/labels",
        "body": {"labels": ["triage"]},
        "fields": ["labels"],
        "target_refs": [f"issue:example-org/widgets#{issue}"],
    }
    if readback:
        action["readback"] = {"path": f"{REPO}/issues/{issue}", "field_path": "labels"}
    return action


def _issues(case: str, index: int, n: int) -> Built:
    issue = str(9000 + n)
    comment_id = (9000 + n) * 10 + 1  # a comment's own id, distinct from its issue number
    action_id = f"widgets-{issue}"
    template, stale = COMMENTS[index % len(COMMENTS)]
    text = template.format(record=issue)
    posted = _ok({"id": comment_id, "body": text}, 201)
    if case == "verified":
        return _comment(action_id, issue, text), posted, _ok({"id": comment_id, "body": text})
    if case == "mismatch_value":
        return _comment(action_id, issue, text), posted, _ok({"id": comment_id, "body": stale})
    if case == "mismatch_missing":
        return _comment(action_id, issue, text), posted, _ok({"id": comment_id, "user": {"login": "widgets-bot"}})
    if case == "external":
        return _comment(action_id, issue, ISSUES_EXTERNAL[index].format(record=issue)), _tripwire(), None
    if case == "method":
        action = {
            "id": action_id,
            "kind": "update",
            "provider": "github",
            "method": "DELETE",
            "path": f"{REPO}/issues/{issue}/labels/triage",
            "target_refs": [f"issue:example-org/widgets#{issue}"],
        }
        return action, _tripwire(), None
    if case == "control_plane":
        action = {**_comment(action_id, issue, text), "path": f"/_admin{REPO}/issues/{issue}/comments"}
        del action["readback"]
        return action, _tripwire(), None
    if case == "failed":
        return _labels(action_id, issue), _ok({"message": "Server Error"}, 500), None
    if case == "readback_404":
        return _comment(action_id, issue, text), posted, None
    if case == "no_readback":
        return _labels(action_id, issue, readback=False), _ok([{"name": "triage"}]), None
    raise ValueError(f"no {case!r} case for {ISSUES}")


BUILDERS = {CRM: _crm, BILLING: _billing, ISSUES: _issues}


# ---- assembly -------------------------------------------------------------------------------------------------


def build_fixture() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The header (`sessions.json`) and the 100 calls (`calls.jsonl`), as the JSON values the files hold."""
    queues = [
        (0, _deal([(row.start, [Slot(session, row.case, i) for i in range(row.size)]) for row in rows]))
        for session, rows in PLANS.items()
    ]
    replays = {
        session: next((row.replays for row in rows if row.case == "replay"), ()) for session, rows in PLANS.items()
    }
    verified: dict[tuple[str, int], dict[str, Any]] = {}
    calls: list[dict[str, Any]] = []
    for position, slot in enumerate(_deal(queues)):
        n = position + 1
        if slot.case == "replay":
            original = verified[(slot.session, replays[slot.session][slot.ordinal])]
            action, write, read = original["action"], original["write"], original["read"]
        else:
            action, write, read = BUILDERS[slot.session](slot.case, slot.ordinal, n)
        call = {"n": n, "session": slot.session, "action": action, "write": write, "read": read,
                "expect": EXPECT[slot.case]}  # fmt: skip
        if slot.case == "verified":
            verified[(slot.session, slot.ordinal)] = call
        calls.append(call)
    for session, rows in PLANS.items():
        for row in rows:
            if row.case == "replay" and len(row.replays) != row.size:
                raise ValueError(f"{session}: the replay row names {len(row.replays)} calls but counts {row.size}")
    if len(calls) != 100 or Counter(call["expect"] for call in calls) != MIX:
        raise ValueError(f"the plan deals {len(calls)} calls: {Counter(call['expect'] for call in calls)}")
    header = {"workspace": WORKSPACE, "clock": CLOCK, "sessions": SESSIONS}
    # Round-trip through JSON so the result compares equal to what the committed files load as (lists, not tuples).
    return json.loads(json.dumps(header)), json.loads(json.dumps(calls))


def main() -> int:
    header, calls = build_fixture()
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    (FIXTURE_DIR / "sessions.json").write_text(json.dumps(header, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (FIXTURE_DIR / "calls.jsonl").write_text(
        "".join(json.dumps(call, sort_keys=True) + "\n" for call in calls), encoding="utf-8"
    )
    counts = Counter(call["expect"] for call in calls)
    print(f"wrote {len(calls)} calls to {FIXTURE_DIR.relative_to(ROOT)}: {dict(sorted(counts.items()))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
