"""Shared fixtures.

These build a *synthetic* context that resembles a mid-run Benchpress state. Nothing here
is imported by `src/benchpress`, and nothing here comes from the benchmark's seeds or
verifier — the names are invented so the gate tests prove generic behaviour.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from benchpress.context import (
    Action,
    Candidate,
    Context,
    DefinitionOfDone,
    EndStateItem,
    Plan,
    ReadBack,
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
    readback: ReadBack | None = None,
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
            "readback": readback.model_dump() if readback else None,
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


ARGA_FORCE_ENV = "BENCHPRESS_ARGA_TESTS"
ARGA_DEPENDENT = (
    "tests/devsim/test_candidates.py",
    "tests/devsim/test_lifecycle.py",
    "tests/devsim/test_scaffold.py",
    "tests/test_harness_bridge.py",
    "tests/test_scenarios.py",
)
_REPO_ROOT = Path(__file__).resolve().parents[1]


def _harness_present() -> bool:
    from evals.harness_bridge import DEFAULT_HARNESS_DIRNAME, HARNESS_ROOT_ENV

    override = os.environ.get(HARNESS_ROOT_ENV)
    root = Path(override).expanduser() if override else _REPO_ROOT / DEFAULT_HARNESS_DIRNAME
    return (root / "src").exists()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Tests that import the vendored ArgaBench harness skip unless it is present (or forced)."""
    if _harness_present():
        return
    forced = os.environ.get(ARGA_FORCE_ENV) == "1"
    skip = pytest.mark.skip(reason="vendored ArgaBench harness not present; set BENCHPRESS_ARGA_TESTS=1 to require it")
    for item in items:
        try:
            rel = Path(str(item.fspath)).resolve().relative_to(_REPO_ROOT)
        except ValueError:
            continue
        if rel.as_posix().startswith(ARGA_DEPENDENT) and not forced:
            item.add_marker(skip)
