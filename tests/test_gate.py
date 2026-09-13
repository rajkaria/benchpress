"""The authority layer's contract.

Each case asserts a thing that *cannot happen*, independent of what any model asked for.
The fixture entities are invented; none of these tests reference a benchmark task.
"""

from __future__ import annotations

import pytest

from benchpress.context import Action, Context, DefinitionOfDone, Plan
from benchpress.gate import Gate, GateRefusal, classify, fingerprint
from tests.conftest import make_action


def _gate(context: Context, *, allow_unplanned: bool = True) -> Gate:
    return Gate(context=context, allow_unplanned=allow_unplanned)


def _refusal(context: Context, action: Action, *, allow_unplanned: bool = True) -> str:
    verdict = _gate(context, allow_unplanned=allow_unplanned).evaluate(action)
    assert not verdict.allowed, f"expected refusal, got allow for {action.path}"
    return verdict.rule


def _allowed(context: Context, action: Action, *, allow_unplanned: bool = True) -> None:
    verdict = _gate(context, allow_unplanned=allow_unplanned).evaluate(action)
    assert verdict.allowed, f"expected allow, refused by {verdict.rule}: {verdict.reason}"


# -- control plane ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/admin",
        "/admin/state",
        "/_admin/reset",
        "/_twin/state",
        "/inspect",
        "/reset",
        "/.well-known/openid-configuration",
        "/openapi.json",
        "/swagger/index.html",
        "/schema",
        "/health",
        "/healthz",
        "/readyz",
        "/metrics",
        "/docs",
        "/",
        "/api",
    ],
)
def test_control_plane_paths_are_refused(context: Context, path: str) -> None:
    assert _refusal(context, make_action(path=path, method="GET", kind="read")) == "control_plane"


def test_absolute_urls_are_refused(context: Context) -> None:
    assert _refusal(context, make_action(path="https://api.stripe.com/v1/customers")) == "control_plane"


def test_graphql_introspection_is_refused(context: Context) -> None:
    action = make_action(provider="linear", method="POST", path="/graphql", body={"query": "{ __schema { types } }"})
    assert _refusal(context, action) == "control_plane"


# -- method ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "path"),
    [
        ("hubspot", "/crm/v3/objects/companies/701"),
        ("stripe", "/v1/customers/cus_R1"),
        ("gmail", "/gmail/v1/users/me/drafts/d1"),
        ("github", "/repos/acme/app/issues/4"),
    ],
)
def test_delete_is_never_permitted(context: Context, provider: str, path: str) -> None:
    assert _refusal(context, make_action(provider=provider, method="DELETE", path=path)) == "method"


# -- action classes --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "method", "path", "body", "expected"),
    [
        ("gmail", "POST", "/gmail/v1/users/me/messages/send", {"raw": "..."}, "send_email"),
        ("gmail", "POST", "/gmail/v1/users/me/drafts/send", {"id": "d1"}, "send_email"),
        ("gmail", "POST", "/gmail/v1/users/me/drafts/d1/send", None, "send_email"),
        ("stripe", "POST", "/v1/charges", {"amount": 100}, "create_charge"),
        ("stripe", "POST", "/v1/payment_intents", {"amount": 100}, "create_charge"),
        ("stripe", "POST", "/v1/refunds", {"charge": "ch_1"}, "create_charge"),
        ("stripe", "POST", "/v1/invoices", {"customer": "cus_R1"}, "create_invoice"),
        ("stripe", "POST", "/v1/invoices/in_1/pay", None, "create_invoice"),
        ("stripe", "POST", "/v1/subscriptions", {"customer": "cus_R1"}, "update_subscription"),
        ("stripe", "DELETE", "/v1/subscriptions/sub_1", None, "delete_any"),
    ],
)
def test_forbidden_action_classes_are_refused(
    context: Context, provider: str, method: str, path: str, body: object, expected: str
) -> None:
    action = make_action(provider=provider, method=method, path=path, body=body)
    assert expected in classify(provider, method, path, body)
    rule = _refusal(context, action)
    assert rule in {"action_class", "method"}


def test_gmail_label_smuggling_to_sent_is_refused(context: Context) -> None:
    action = make_action(
        provider="gmail",
        method="POST",
        path="/gmail/v1/users/me/messages/m1/modify",
        body={"addLabelIds": ["SENT"]},
    )
    assert _refusal(context, action) == "action_class"


def test_github_merge_is_refused_when_forbidden(context: Context) -> None:
    context.dod = context.dod.model_copy(update={"forbidden": (*context.dod.forbidden, "merge_pr")})
    context.dod = context.dod.model_copy(update={"write_scope": (*context.dod.write_scope, "github")})
    action = make_action(provider="github", method="PUT", path="/repos/acme/app/pulls/12/merge", body={})
    assert _refusal(context, action) == "action_class"


def test_github_workflow_disable_is_refused_when_forbidden(context: Context) -> None:
    context.dod = context.dod.model_copy(
        update={
            "forbidden": (*context.dod.forbidden, "disable_workflow"),
            "write_scope": (*context.dod.write_scope, "github"),
        }
    )
    action = make_action(provider="github", method="PUT", path="/actions/workflows/ci.yml/disable")
    assert _refusal(context, action) == "action_class"


def test_calendar_hold_with_attendees_is_refused_when_forbidden(context: Context) -> None:
    context.dod = context.dod.model_copy(
        update={
            "forbidden": (*context.dod.forbidden, "calendar_invite_attendees"),
            "write_scope": (*context.dod.write_scope, "google_calendar"),
        }
    )
    action = make_action(
        provider="google_calendar",
        method="POST",
        path="/calendars/primary/events",
        body={"summary": "Hold", "attendees": [{"email": "someone@rivermill.example"}]},
    )
    assert _refusal(context, action) == "action_class"


def test_calendar_hold_without_attendees_is_allowed(context: Context) -> None:
    context.dod = context.dod.model_copy(
        update={
            "forbidden": (*context.dod.forbidden, "calendar_invite_attendees"),
            "write_scope": (*context.dod.write_scope, "google_calendar"),
        }
    )
    action = make_action(
        provider="google_calendar",
        method="POST",
        path="/calendars/primary/events",
        body={"summary": "Internal hold"},
    )
    _allowed(context, action)


def test_gmail_draft_creation_is_allowed(context: Context) -> None:
    """The draft primitive must stay reachable — refusing it would fail the task."""
    action = make_action(
        provider="gmail",
        method="POST",
        path="/gmail/v1/users/me/drafts",
        body={"message": {"raw": "VG8gYXA="}},
    )
    _allowed(context, action)


# -- protected set ---------------------------------------------------------------------


def test_protected_name_in_body_is_refused(context: Context) -> None:
    action = make_action(body={"properties": {"description": "Updated Rivermill Studios Prospect contact"}})
    assert _refusal(context, action) == "protected"


def test_protected_domain_in_body_is_refused(context: Context) -> None:
    action = make_action(body={"properties": {"domain": "rivermill-studios.example"}})
    assert _refusal(context, action) == "protected"


def test_protected_id_in_path_is_refused(context: Context) -> None:
    context.protected.ids.add("cus_PROSPECT9")
    action = make_action(
        provider="stripe", method="POST", path="/v1/customers/cus_PROSPECT9", body={"email": "x@rivermill.example"}
    )
    assert _refusal(context, action) == "protected"


def test_chosen_target_is_not_blocked_by_its_own_lookalike(context: Context) -> None:
    """`Rivermill Studio` must remain writable even though `Rivermill Studios Prospect`
    is protected — otherwise the deny-list would refuse the real work."""
    action = make_action(body={"properties": {"description": "Rivermill Studio now bills ap@rivermill.example"}})
    _allowed(context, action)


def test_reads_are_not_blocked_by_the_protected_set(context: Context) -> None:
    """Enumeration has to be able to read the look-alike in order to lock it."""
    action = make_action(method="GET", kind="read", path="/crm/v3/objects/companies/702", satisfies=())
    _allowed(context, action)


# -- provider scope --------------------------------------------------------------------


def test_write_outside_scope_is_refused(context: Context) -> None:
    action = make_action(provider="linkedin", method="POST", path="/ugcPosts", body={"text": "hello"})
    assert _refusal(context, action) == "provider_scope"


def test_read_outside_scope_is_allowed(context: Context) -> None:
    action = make_action(provider="linkedin", method="GET", kind="read", path="/ugcPosts", satisfies=())
    _allowed(context, action)


# -- plan membership -------------------------------------------------------------------


def test_unplanned_write_is_refused(context: Context) -> None:
    action = make_action("a99", body={"properties": {"description": "ap@rivermill.example"}})
    assert _refusal(context, action, allow_unplanned=False) == "plan_membership"


def test_planned_write_is_allowed(context: Context) -> None:
    _allowed(context, context.plan.actions[0], allow_unplanned=False)


def test_planned_action_with_switched_endpoint_is_refused(context: Context) -> None:
    action = context.plan.actions[0].model_copy(update={"path": "/crm/v3/objects/deals/701"})
    assert _refusal(context, action, allow_unplanned=False) == "plan_membership"


def test_action_satisfying_nothing_is_refused(context: Context) -> None:
    bare = make_action("a2", satisfies=(), body={"properties": {"description": "x"}})
    context.plan = Plan(actions=(*context.plan.actions, bare))
    assert _refusal(context, bare, allow_unplanned=False) == "plan_membership"


# -- field smuggling -------------------------------------------------------------------


def test_undeclared_field_is_refused(context: Context) -> None:
    action = make_action(
        body={"properties": {"description": "ap@rivermill.example", "lifecyclestage": "customer"}},
        fields=("description",),
    )
    assert _refusal(context, action) == "field_smuggling"


def test_declared_fields_pass(context: Context) -> None:
    action = make_action(
        body={"properties": {"description": "ap@rivermill.example"}},
        fields=("description",),
    )
    _allowed(context, action)


def test_wrapper_keys_are_transparent(context: Context) -> None:
    action = make_action(
        body={"properties": {"description": "ap@rivermill.example"}},
        fields=("description",),
    )
    _allowed(context, action)


# -- external destinations -------------------------------------------------------------


def test_external_recipient_is_refused(context: Context) -> None:
    action = make_action(
        provider="gmail",
        method="POST",
        path="/gmail/v1/users/me/drafts",
        body={"message": {"to": "collector@evil.example", "body": "invoices"}},
    )
    assert _refusal(context, action) == "external_destination"


def test_external_url_is_refused(context: Context) -> None:
    action = make_action(
        body={"properties": {"description": "see https://exfil.example/inbox"}}, fields=("description",)
    )
    assert _refusal(context, action) == "external_destination"


def test_known_customer_domain_is_allowed(context: Context) -> None:
    action = make_action(
        body={"properties": {"description": "billing moves to ap@rivermill.example"}},
        fields=("description",),
    )
    _allowed(context, action)


# -- idempotency -----------------------------------------------------------------------


def test_replayed_fingerprint_is_refused(context: Context) -> None:
    action = context.plan.actions[0]
    gate = _gate(context)
    gate.check(action)
    gate.record_success(fingerprint(action))
    with pytest.raises(GateRefusal) as excinfo:
        gate.check(action)
    assert excinfo.value.rule == "idempotency"


def test_fingerprint_is_stable_across_formatting(context: Context) -> None:
    first = make_action(body={"properties": {"description": "AP@Rivermill.Example"}})
    second = make_action(body={"properties": {"description": "ap@rivermill.example"}})
    assert fingerprint(first) == fingerprint(second)


def test_fingerprint_differs_on_a_different_target(context: Context) -> None:
    first = make_action(path="/crm/v3/objects/companies/701")
    second = make_action(path="/crm/v3/objects/companies/999")
    assert fingerprint(first) != fingerprint(second)


# -- refusals are recorded -------------------------------------------------------------


def test_refusal_is_recorded_on_the_context(context: Context) -> None:
    gate = _gate(context)
    with pytest.raises(GateRefusal):
        gate.check(make_action(provider="stripe", method="POST", path="/v1/charges", body={"amount": 1}))
    assert context.refusals
    assert context.refusals[-1].rule == "action_class"


def test_empty_definition_of_done_still_blocks_deletes(context: Context) -> None:
    context.dod = DefinitionOfDone()
    assert _refusal(context, make_action(method="DELETE")) == "method"
