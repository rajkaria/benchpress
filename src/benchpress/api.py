"""The public entry point: `wrap(model, executor)` puts the Benchpress loop around any tool layer.

    agent = benchpress.wrap("deepseek-v4-pro", gateway.execute_tool, providers=["hubspot", "stripe"])
    result = await agent.run("Move Acme's renewal notices to ap@acme.example")

The executor is anything with the harness `execute_tool(tool_name, tool_input)` shape, either a
coroutine function or an object exposing `execute_tool`. Every write it receives has already passed
the gate; every run ends with an evidence-only status and, when `trace_dir` is set, a receipt.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, TypeAlias, runtime_checkable

from benchpress.controller import TrialResult, run_trial
from benchpress.model import ModelClient, ModelConfig, ModelTransport
from benchpress.packs import PolicyPack
from benchpress.phases.common import Ablations
from benchpress.playbooks import Playbook
from benchpress.prompts import system_text
from benchpress.tools import ToolExecutor

DEFAULT_SYSTEM_PROMPT = (
    "You are an operations agent working across the provisioned business systems. Complete the request "
    "through the provider_api tool using ordinary data-plane routes only. Minimize mutations, respect every "
    "explicit prohibition, verify the final state through reads, and return exactly the output requested."
)


@runtime_checkable
class HasExecuteTool(Protocol):
    def execute_tool(self, tool_name: str, tool_input: dict[str, Any]) -> Awaitable[object]: ...


ExecutorLike: TypeAlias = ToolExecutor | HasExecuteTool
ModelLike: TypeAlias = str | ModelConfig | None


@dataclass(frozen=True)
class Benchpress:
    """A configured loop. Stateless between runs: each `run` gets a fresh context, gate and model client."""

    executor: ToolExecutor
    providers: tuple[str, ...]
    config: ModelConfig
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    ablations: Ablations = field(default_factory=Ablations)
    playbooks: Mapping[str, Playbook] | None = None
    transport: ModelTransport | None = None
    policy_packs: tuple[PolicyPack, ...] = ()

    async def run(
        self,
        request: str,
        *,
        trace_dir: str | Path | None = None,
        trial_id: str | None = None,
    ) -> TrialResult:
        if not request.strip():
            raise ValueError("request must be a non-empty string")
        client = None
        if self.transport is not None:
            client = ModelClient(self.config, system_text(self.system_prompt), transport=self.transport)
        return await run_trial(
            system_prompt=self.system_prompt,
            user_prompt=request,
            providers=self.providers,
            execute_tool=self.executor,
            config=self.config,
            ablations=self.ablations,
            trace_dir=Path(trace_dir) if trace_dir is not None else None,
            trial_id=trial_id,
            model_client=client,
            playbooks=self.playbooks,
            policy_packs=self.policy_packs,
        )

    def run_sync(
        self,
        request: str,
        *,
        trace_dir: str | Path | None = None,
        trial_id: str | None = None,
    ) -> TrialResult:
        """`run` for synchronous callers. Raises if called from inside a running event loop."""
        return asyncio.run(self.run(request, trace_dir=trace_dir, trial_id=trial_id))


def _config(model: ModelLike) -> ModelConfig:
    if isinstance(model, ModelConfig):
        return model
    return ModelConfig.from_env(model=model)


def _executor(executor: ExecutorLike) -> ToolExecutor:
    if isinstance(executor, HasExecuteTool):
        return executor.execute_tool
    if callable(executor):
        return executor
    raise TypeError("executor must be an async callable (tool_name, tool_input) or expose execute_tool")


def wrap(
    model: ModelLike,
    executor: ExecutorLike,
    *,
    providers: Sequence[str],
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    ablations: Ablations | None = None,
    playbooks: Mapping[str, Playbook] | None = None,
    transport: ModelTransport | None = None,
    policy_packs: Sequence[PolicyPack] = (),
) -> Benchpress:
    """Wrap a tool layer in the Benchpress loop.

    `model` is a model id (credentials from the environment: `DEEPSEEK_API_KEY`/`BENCHPRESS_API_KEY`
    for OpenAI-compatible endpoints, `ANTHROPIC_API_KEY` for `claude-*`), a full `ModelConfig`, or
    None for `BENCHPRESS_MODEL`. `providers` names the systems the executor can reach; built-in
    playbooks cover slack, gmail, hubspot and stripe, and `playbooks` supplies your own. `transport`
    swaps the model wire protocol (bring your own LLM client, or a scripted one in tests). `policy_packs`
    adds code-enforced rule sets (`benchpress.load_policy_packs(["billing"])`); packs only ever add refusals.
    """
    names = tuple(name.strip() for name in providers if name.strip())
    if not names:
        raise ValueError("providers must name at least one system the executor can reach")
    return Benchpress(
        executor=_executor(executor),
        providers=names,
        config=_config(model),
        system_prompt=system_prompt,
        ablations=ablations or Ablations(),
        playbooks=playbooks,
        transport=transport,
        policy_packs=tuple(policy_packs),
    )
