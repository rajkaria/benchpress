"""Shared fixtures.

These build a *synthetic* context that resembles a mid-run Benchpress state. Nothing here
is imported by `src/benchpress`, and nothing here comes from the benchmark's seeds or
verifier — the names are invented so the gate tests prove generic behaviour.
"""

from __future__ import annotations

import pytest

from benchpress.context import (
    Action,
    Candidate,
    Context,
    DefinitionOfDone,
    EndStateItem,
    Plan,
    ResolvedTarget,
)


def make_action(
    action_id: str = "a1",
    *,
    provider: str = "hubspot",
    method: str = "PATCH",
    path: str = "/crm/v3/objects/companies/701",
    body: object | None = None,
    fields: tuple[str, ...] = (),
    satisfies: tuple[str, ...] = ("end_state[0]",),
    kind: str = "update",
    query: dict[str, str] | None = None,
) -> Action:
    return Action.model_validate(
        {
            "id": action_id,
            "kind": kind,
            "provider": provider,
            "method": method,
            "path": path,
            "body": body,
            "fields": fields,
            "satisfies": satisfies,
            "query": query or {},
        }
    )


@pytest.fixture
def context() -> Context:
    """A resolved run: one chosen company, one look-alike locked out."""
    ctx = Context(
        trial_id="t-test",
        user_prompt=(
            "Marlon posted in the #ops-desk channel: Rivermill Studio asked for renewal "
            "notices to move to its accounts-payable address at ap@rivermill.example. "
            "Do not create charges, move subscriptions, or send external mail."
        ),
        providers=("hubspot", "stripe", "slack", "gmail"),
    )
    chosen = Candidate(
        provider="hubspot",
        resource_type="company",
        resource_id="701",
        display="Rivermill Studio",
        name="Rivermill Studio",
        domain="rivermill.example",
        email="ap@rivermill.example",
        lifecycle="customer",
    )
    lookalike = Candidate(
        provider="hubspot",
        resource_type="company",
        resource_id="702",
        display="Rivermill Studios Prospect",
        name="Rivermill Studios Prospect",
        domain="rivermill-studios.example",
        lifecycle="prospect",
    )
    ctx.candidates = [chosen, lookalike]
    ctx.protected.add_candidate(lookalike)
    ctx.targets = [
        ResolvedTarget(
            provider="hubspot",
            resource_type="company",
            resource_id="701",
            display="Rivermill Studio",
            evidence=("lifecycle=customer", "domain=rivermill.example"),
        ),
        ResolvedTarget(
            provider="stripe",
            resource_type="customer",
            resource_id="cus_R1",
            display="Rivermill Studio",
            evidence=("email=billing@rivermill.example",),
        ),
    ]
    ctx.dod = DefinitionOfDone(
        end_state=(
            EndStateItem(
                provider="hubspot",
                resource="company:701",
                field="description",
                expected="ap@rivermill.example",
            ),
        ),
        forbidden=(
            "send_email",
            "create_charge",
            "create_invoice",
            "update_subscription",
            "move_subscription",
            "delete_any",
            "mutate_protected",
        ),
        write_scope=("hubspot", "stripe", "slack", "gmail"),
        facts={
            "customer": "Rivermill Studio",
            "former_contact": "billing@rivermill.example",
            "verified_contact": "ap@rivermill.example",
        },
    )
    ctx.plan = Plan(
        actions=(
            make_action(
                "a1",
                body={"properties": {"description": "Billing contact is ap@rivermill.example"}},
                fields=("description",),
            ),
        )
    )
    return ctx
