"""Benchpress — the reliability layer for AI agents with write access.

Policy-first, authority-gated, evidence-verified execution for multi-app agents.
Nothing in this package is keyed to a task id, a seeded name, or a seeded domain:
every task fact is discovered at runtime from the prompt and from provider reads.

    import benchpress

    agent = benchpress.wrap("deepseek-v4-pro", gateway.execute_tool, providers=["hubspot", "stripe"])
    result = await agent.run("Move Acme's renewal notices to ap@acme.example")
"""

from __future__ import annotations

from benchpress.api import DEFAULT_SYSTEM_PROMPT, Benchpress, wrap
from benchpress.controller import TrialResult, run_trial
from benchpress.gate import Gate, GateRefusal
from benchpress.model import ModelConfig
from benchpress.phases.common import Ablations
from benchpress.tools import ToolExecutor

__version__ = "0.2.0"

__all__ = [
    "DEFAULT_SYSTEM_PROMPT",
    "Ablations",
    "Benchpress",
    "Gate",
    "GateRefusal",
    "ModelConfig",
    "ToolExecutor",
    "TrialResult",
    "__version__",
    "run_trial",
    "wrap",
]
