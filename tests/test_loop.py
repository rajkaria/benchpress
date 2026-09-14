"""Standalone phases: each runs its missing predecessors, so policy-first cannot be skipped."""

from __future__ import annotations

from benchpress.controller import run_trial
from benchpress.demo import PROMPT, GmailBook, HubSpotBook, ScriptedTransport, SlackBook, StripeBook, Workspace
from benchpress.loop import PHASE_ORDER, build_deps, definition_of_done, resolve_targets, run_phases, verify
from benchpress.model import ModelClient, ModelConfig
from benchpress.playbooks import Playbook

PROVIDERS = ("slack", "gmail", "hubspot", "stripe")


def _books() -> dict[str, Playbook]:
    return {"slack": SlackBook(), "gmail": GmailBook(), "hubspot": HubSpotBook(), "stripe": StripeBook()}


def _deps(workspace: Workspace):
    model = ModelClient(ModelConfig(model="scripted"), "sys", transport=ScriptedTransport())
    return build_deps(PROMPT, workspace.execute_tool, providers=PROVIDERS, model=model, playbooks=_books())


async def test_resolve_targets_runs_orient_and_the_policy_sweep_first() -> None:
    deps = _deps(Workspace())
    targets = await resolve_targets(deps)
    assert deps.completed == ["orient", "policy_sweep", "resolve"]
    assert targets and all(t.resource_id for t in targets)
    assert deps.ctx.policies, "the sweep ran before resolution"


async def test_a_phase_that_already_ran_is_not_run_twice() -> None:
    deps = _deps(Workspace())
    await resolve_targets(deps)
    dod = await definition_of_done(deps)
    assert deps.completed == ["orient", "policy_sweep", "resolve", "define_done"]
    assert dod.end_state


async def test_verify_after_standalone_calls_matches_run_trial() -> None:
    deps = _deps(Workspace())
    await resolve_targets(deps)
    status = await verify(deps)
    assert deps.completed == list(PHASE_ORDER)
    reference = await run_trial(
        system_prompt="",
        user_prompt=PROMPT,
        providers=PROVIDERS,
        execute_tool=Workspace().execute_tool,
        config=ModelConfig(model="scripted"),
        model_client=ModelClient(ModelConfig(model="scripted"), "sys", transport=ScriptedTransport()),
        playbooks=_books(),
    )
    assert status == reference.status == "completed"


async def test_run_phases_runs_every_phase_once_in_order() -> None:
    deps = _deps(Workspace())
    await run_phases(deps)
    assert deps.completed == list(PHASE_ORDER)
