"""Benchpress — the reliability layer for AI agents with write access.

Policy-first, authority-gated, evidence-verified execution for multi-app agents.
Nothing in this package is keyed to a task id, a seeded name, or a seeded domain:
every task fact is discovered at runtime from the prompt and from provider reads.

    import benchpress

    agent = benchpress.wrap("deepseek-v4-pro", gateway.execute_tool, providers=["hubspot", "stripe"])
    result = await agent.run("Move Acme's renewal notices to ap@acme.example")

Rehearse lives in `benchpress.rehearse` (`rehearse`, `replay`); the types are re-exported here.
"""

from __future__ import annotations

from benchpress.api import DEFAULT_SYSTEM_PROMPT, Benchpress, wrap
from benchpress.controller import TrialResult, run_trial
from benchpress.gate import Gate, GateRefusal, PolicyRuleSet
from benchpress.gate_corpus import GateCase, load_corpus, run_case
from benchpress.model import ModelConfig
from benchpress.packs import PolicyPack, available_policy_packs, load_policy_pack, load_policy_packs
from benchpress.phases.common import Ablations
from benchpress.rehearse import (
    Divergence,
    Normalizer,
    PlannedWrite,
    Rehearsal,
    ReplayReceipt,
    RunRecord,
    Stage,
    StageFactory,
    state_hash,
)
from benchpress.tools import ToolExecutor

__version__ = "0.5.0"

__all__ = [
    "Ablations",
    "Benchpress",
    "DEFAULT_SYSTEM_PROMPT",
    "Divergence",
    "Gate",
    "GateCase",
    "GateRefusal",
    "ModelConfig",
    "Normalizer",
    "PlannedWrite",
    "PolicyPack",
    "PolicyRuleSet",
    "Rehearsal",
    "ReplayReceipt",
    "RunRecord",
    "Stage",
    "StageFactory",
    "ToolExecutor",
    "TrialResult",
    "__version__",
    "available_policy_packs",
    "load_corpus",
    "load_policy_pack",
    "load_policy_packs",
    "run_case",
    "run_trial",
    "state_hash",
    "wrap",
]
