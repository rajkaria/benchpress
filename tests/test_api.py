"""The public package surface: `wrap(model, executor)` drives the full loop, and the metadata is shippable."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

import pytest

import benchpress
from benchpress import Ablations, Benchpress, ModelConfig, load_policy_packs, wrap
from benchpress.demo import (
    PROMPT,
    GmailBook,
    HubSpotBook,
    ScriptedTransport,
    SlackBook,
    StripeBook,
    Workspace,
)

PROVIDERS = ["slack", "gmail", "hubspot", "stripe"]
BOOKS = {"slack": SlackBook(), "gmail": GmailBook(), "hubspot": HubSpotBook(), "stripe": StripeBook()}
ROOT = Path(__file__).resolve().parents[1]


def _agent(executor: Any) -> Benchpress:
    return wrap(
        ModelConfig(model="scripted"),
        executor,
        providers=PROVIDERS,
        playbooks=BOOKS,
        transport=ScriptedTransport(),
    )


@pytest.mark.asyncio
async def test_wrap_runs_the_loop_over_a_plain_executor_function(tmp_path: Path) -> None:
    workspace = Workspace()
    result = await _agent(workspace.execute_tool).run(PROMPT, trace_dir=tmp_path, trial_id="api-1")
    assert result.status == "completed" and result.error is None
    assert workspace.customers["cus_R1"]["email"] == "ap@rivermill.example"
    assert workspace.companies["702"]["description"] == "never purchased", "the gate kept the look-alike untouched"
    assert any(v.rule == "protected" for v in result.context.refusals)
    receipt = json.loads((tmp_path / "receipt.json").read_text())
    assert receipt, "a receipt is written when trace_dir is set"


@pytest.mark.asyncio
async def test_wrap_accepts_an_object_exposing_execute_tool() -> None:
    workspace = Workspace()
    result = await _agent(workspace).run(PROMPT)
    assert result.status == "completed"
    assert result.provider_calls == len(workspace.calls)


def test_run_sync_for_synchronous_callers() -> None:
    workspace = Workspace()
    result = _agent(workspace).run_sync(PROMPT)
    assert result.status == "completed"


def test_wrap_resolves_model_ids_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    agent = wrap("claude-opus-5", Workspace(), providers=["stripe"])
    assert agent.config.provider == "anthropic" and agent.config.model == "claude-opus-5"
    assert agent.providers == ("stripe",) and agent.ablations == Ablations()


def test_wrap_rejects_empty_providers_and_bad_executors() -> None:
    with pytest.raises(ValueError, match="providers"):
        wrap(ModelConfig(model="scripted"), Workspace(), providers=[" "])
    with pytest.raises(TypeError, match="executor"):
        wrap(ModelConfig(model="scripted"), object(), providers=["stripe"])  # pyright: ignore[reportArgumentType]


@pytest.mark.asyncio
async def test_run_rejects_an_empty_request() -> None:
    with pytest.raises(ValueError, match="request"):
        await _agent(Workspace()).run("   ")


def test_public_surface_and_version_match_the_distribution() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["name"] == "benchpress-agent"
    assert benchpress.__version__ == project["version"]
    for name in ("wrap", "Benchpress", "run_trial", "TrialResult", "ModelConfig", "Ablations", "Gate", "GateRefusal"):
        assert name in benchpress.__all__ and hasattr(benchpress, name)
    assert (ROOT / "src" / "benchpress" / "py.typed").exists()
    assert (ROOT / "LICENSE").read_text().lstrip().startswith("Apache License")


@pytest.mark.asyncio
async def test_wrap_passes_policy_packs_through_to_the_gate() -> None:
    packs = load_policy_packs(["billing"])
    agent = wrap(
        ModelConfig(model="scripted"),
        Workspace(),
        providers=PROVIDERS,
        playbooks=BOOKS,
        transport=ScriptedTransport(),
        policy_packs=packs,
    )
    assert agent.policy_packs == tuple(packs) and len(packs) == 1
    result = await agent.run(PROMPT)
    assert result.error is None
