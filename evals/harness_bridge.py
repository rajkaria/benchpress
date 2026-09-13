"""The only Benchpress module that touches the vendored ArgaBench harness.

Everything `evals/` borrows from `arga-twins-benchmark` passes through here, so one file shows exactly
what is taken from the judges' harness and how faithfully:

- `system_prompt()`     `arga_twins_benchmark.runner.prompting.SYSTEM_PROMPT`, verbatim.
- `user_prompt(task)`   the suite task's `prompt`, verbatim. `scripts/run_argabench_40.py` passes
                        `user_prompt=task["prompt"]` straight into `invoke_model` and records the same
                        string in `prompt.json`; it never calls `compose_user_prompt`, so no structured
                        output instruction is appended for this suite.
- `tool_schema(...)`    `[provider_api, provider_docs]`, built by the harness's own `ProviderGateway` and
                        `OfficialDocsGateway` constructors, in the order the script hands them to the model.
- `provider_roles()`    the script's `PROVIDER_ROLES` table, read from its source with `ast` so it cannot
                        drift from the file the judges run (and without executing the script).
- `task_spec(...)`      prompt, twins, facts, protected terms and allowed providers from `suite.json`.
- `docs_executor(...)`  a `provider_docs` executor backed by `OfficialDocsGateway`. The docs gateway needs
                        only the catalog bundled with the harness (`search`) and the public internet
                        (`fetch`); it never talks to Arga, so it runs without any Arga credentials.

The harness is imported dynamically (`importlib`) rather than statically: it lives behind a gitignored
symlink, is not a declared dependency, and `src/benchpress` must never import it or this module.
"""

from __future__ import annotations

import ast
import copy
import importlib
import json
import logging
import os
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import httpx

ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[object]]

REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS_ROOT_ENV = "ARGABENCH_ROOT"
DEFAULT_HARNESS_DIRNAME = "arga-twins-benchmark"
SUITE_PATH = Path("benchmark", "argabench_40", "suite.json")
RUN_SCRIPT_PATH = Path("scripts", "run_argabench_40.py")
EXPECTED_TASK_COUNT = 40
PROVIDER_API_TOOL = "provider_api"
PROVIDER_DOCS_TOOL = "provider_docs"
_SCRIPT_CONSTANTS = ("PROVIDER_ROLES", "PROVIDER_TOOL_LIMIT", "OFFICIAL_DOCS_TOOL_LIMIT", "MODEL_TIMEOUT_SECONDS")

_log = logging.getLogger(__name__)


class HarnessNotFoundError(RuntimeError):
    """The vendored ArgaBench checkout is missing or incomplete."""


# --------------------------------------------------------------------------------------- location


def harness_root() -> Path:
    """`$ARGABENCH_ROOT` or `<repo>/arga-twins-benchmark`; raises with the fix if it is not a harness checkout."""
    override = os.environ.get(HARNESS_ROOT_ENV, "").strip()
    root = Path(override).expanduser() if override else REPO_ROOT / DEFAULT_HARNESS_DIRNAME
    required = (Path("src", "arga_twins_benchmark"), SUITE_PATH, RUN_SCRIPT_PATH)
    missing = [str(relative) for relative in required if not (root / relative).exists()]
    if missing:
        raise HarnessNotFoundError(
            f"ArgaBench harness not found at {root} (missing: {', '.join(missing)}). "
            f"Symlink the vendored clone with `ln -sfn /path/to/arga-twins-benchmark "
            f"{REPO_ROOT / DEFAULT_HARNESS_DIRNAME}` or set {HARNESS_ROOT_ENV}."
        )
    return root


def ensure_harness_importable() -> None:
    """Put `<root>/src` on `sys.path` once so `arga_twins_benchmark` imports in-process."""
    src = harness_root() / "src"
    wanted = src.resolve()
    if not any(Path(entry).resolve() == wanted for entry in sys.path if entry):
        sys.path.insert(0, str(src))


def _harness_module(name: str) -> ModuleType:
    ensure_harness_importable()
    return importlib.import_module(f"arga_twins_benchmark.{name}")


# ---------------------------------------------------------------------------------------- prompts


def system_prompt() -> str:
    """The harness system prompt, verbatim (`runner/prompting.py: SYSTEM_PROMPT`)."""
    prompt = _harness_module("runner.prompting").SYSTEM_PROMPT
    if not isinstance(prompt, str) or not prompt:
        raise TypeError("harness SYSTEM_PROMPT is not a non-empty string")
    return prompt


def user_prompt(task_id: str) -> str:
    """The user prompt exactly as `scripts/run_argabench_40.py` sends it.

    Composition rule (run_argabench_40.py, `run_task`): `invoke_model(..., user_prompt=task["prompt"], ...)`
    with `task` being the raw `suite.json` entry; `prompt.json` stores the same `task["prompt"]`. Nothing is
    stripped, prefixed or appended. (`compose_user_prompt` in `runner/prompting.py` belongs to the
    catalog-experiment ledger and is not used by this script.)
    """
    prompt = _task(task_id)["prompt"]
    if not isinstance(prompt, str):
        raise TypeError(f"task {task_id!r} prompt is not a string")
    return prompt


# ------------------------------------------------------------------------------------------ suite


@dataclass(frozen=True)
class TaskSpec:
    """One `suite.json` task, with the grading inputs `evals/` needs pulled out of `verification`.

    `facts` are `structured_result.facts` with every value stringified (a few tasks carry integer facts);
    `protected_terms` are `protected_candidate_mutation.selector.reference_any`; `allowed_providers` are
    `mutation_policy.allowed_scope.providers`, in suite order.
    """

    task_id: str
    title: str
    domain: str
    prompt: str
    twins: tuple[str, ...]
    facts: dict[str, str]
    protected_terms: tuple[str, ...]
    allowed_providers: tuple[str, ...]
    minimum_semantic_steps: int
    seed_config: dict[str, Any]
    required_outcomes: list[dict[str, Any]]
    forbidden_outcomes: list[dict[str, Any]]


@cache
def _suite_tasks() -> dict[str, dict[str, Any]]:
    payload: object = json.loads((harness_root() / SUITE_PATH).read_text())
    if not isinstance(payload, dict):
        raise ValueError(f"{SUITE_PATH} must be a JSON object")
    tasks = cast(dict[str, object], payload).get("tasks")
    if not isinstance(tasks, list) or len(cast(list[object], tasks)) != EXPECTED_TASK_COUNT:
        raise ValueError(f"expected {EXPECTED_TASK_COUNT} tasks in {SUITE_PATH}")
    by_id: dict[str, dict[str, Any]] = {}
    for task in cast(list[object], tasks):
        if not isinstance(task, dict):
            raise ValueError("every suite task must be an object")
        typed = cast(dict[str, Any], task)
        by_id[str(typed["id"])] = typed
    return by_id


def _task(task_id: str) -> dict[str, Any]:
    tasks = _suite_tasks()
    if task_id not in tasks:
        raise ValueError(f"unknown ArgaBench task {task_id!r}; known: {', '.join(tasks)}")
    return tasks[task_id]


def all_task_ids() -> tuple[str, ...]:
    return tuple(_suite_tasks())


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"suite field {label!r} must be an object")
    return {str(key): item for key, item in cast(Mapping[object, object], value).items()}


def _strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"suite field {label!r} must be an array")
    return tuple(str(item) for item in cast(list[object], value))


def _outcomes(value: object, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError(f"suite field {label!r} must be an array")
    return [_mapping(item, label) for item in cast(list[object], value)]


def _outcome(outcomes: Sequence[Mapping[str, Any]], outcome_id: str) -> dict[str, Any] | None:
    for outcome in outcomes:
        if outcome.get("id") == outcome_id:
            return dict(outcome)
    return None


def task_spec(task_id: str) -> TaskSpec:
    task = copy.deepcopy(_task(task_id))
    verification = _mapping(task.get("verification"), "verification")
    required = _outcomes(verification.get("required_outcomes"), "required_outcomes")
    forbidden = _outcomes(verification.get("forbidden_outcomes"), "forbidden_outcomes")

    structured = _outcome(required, "structured_result")
    facts_raw = _mapping(structured.get("facts", {}), "facts") if structured else {}
    protected = _outcome(forbidden, "protected_candidate_mutation")
    selector = _mapping(protected.get("selector", {}), "selector") if protected else {}
    policy = _mapping(verification.get("mutation_policy", {}), "mutation_policy")
    scope = _mapping(policy.get("allowed_scope", {}), "allowed_scope")

    return TaskSpec(
        task_id=str(task["id"]),
        title=str(task.get("title", "")),
        domain=str(task.get("domain", "")),
        prompt=user_prompt(task_id),
        twins=_strings(task.get("twins", []), "twins"),
        facts={key: str(value) for key, value in facts_raw.items()},
        protected_terms=_strings(selector.get("reference_any", []), "reference_any"),
        allowed_providers=_strings(scope.get("providers", []), "allowed_scope.providers"),
        minimum_semantic_steps=int(task.get("minimum_semantic_steps", 0)),
        seed_config=_mapping(task.get("seed_config", {}), "seed_config"),
        required_outcomes=required,
        forbidden_outcomes=forbidden,
    )


# ------------------------------------------------------------------------------- script constants


@cache
def _run_script_constants() -> dict[str, Any]:
    """Module-level literals of `scripts/run_argabench_40.py`, read with `ast` (no import, no side effects)."""
    source = (harness_root() / RUN_SCRIPT_PATH).read_text()
    found: dict[str, Any] = {}
    for node in ast.parse(source).body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and target.id in _SCRIPT_CONSTANTS:
            found[target.id] = ast.literal_eval(node.value)
    missing = [name for name in _SCRIPT_CONSTANTS if name not in found]
    if missing:
        raise ValueError(f"{RUN_SCRIPT_PATH} no longer defines {', '.join(missing)}; update evals/harness_bridge.py")
    return found


def provider_roles() -> dict[str, str]:
    """`{provider: role}` exactly as `run_argabench_40.py: PROVIDER_ROLES` (e.g. `slack -> team_chat`)."""
    roles = _mapping(_run_script_constants()["PROVIDER_ROLES"], "PROVIDER_ROLES")
    return {name: str(role) for name, role in roles.items()}


def task_roles(providers: Sequence[str]) -> dict[str, str]:
    """`{role: provider}` for the provisioned providers, like the script's `task_roles(task)`."""
    roles = provider_roles()
    unknown = [provider for provider in providers if provider not in roles]
    if unknown:
        raise ValueError(f"providers without a harness role: {', '.join(unknown)}")
    return {roles[provider]: provider for provider in providers}


def provider_tool_limit() -> int:
    """`PROVIDER_TOOL_LIMIT` (160 provider_api calls per trial)."""
    return int(_run_script_constants()["PROVIDER_TOOL_LIMIT"])


def docs_tool_limit() -> int:
    """`OFFICIAL_DOCS_TOOL_LIMIT` (40 provider_docs calls per trial)."""
    return int(_run_script_constants()["OFFICIAL_DOCS_TOOL_LIMIT"])


def model_timeout_seconds() -> float:
    """`MODEL_TIMEOUT_SECONDS` (1,800 s per trial)."""
    return float(_run_script_constants()["MODEL_TIMEOUT_SECONDS"])


# ------------------------------------------------------------------------------------------ tools


def _provider_names(providers: Sequence[str]) -> list[str]:
    names = list(dict.fromkeys(provider.strip() for provider in providers))
    if not names or any(not name for name in names):
        raise ValueError("providers must be a non-empty sequence of provider names")
    return names


def _tool_definition(value: object, expected_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"harness tool definition for {expected_name!r} is not an object")
    definition = cast(dict[str, Any], value)
    if definition.get("name") != expected_name:
        raise ValueError(f"harness tool definition is named {definition.get('name')!r}, expected {expected_name!r}")
    return definition


def tool_schema(providers: Sequence[str]) -> list[dict[str, Any]]:
    """`[provider_api, provider_docs]` as the harness builds them for a task with these twins.

    Both schemas come from the harness classes themselves (`ProviderGateway.tool_definition`,
    `OfficialDocsGateway.tool_definition`), constructed with placeholder base URLs: the schema depends only
    on the provisioned provider names and their roles, never on twin addresses. The provider enum therefore
    contains the names plus their roles (`slack, team_chat, stripe, payments, ...`), sorted, like a real run.
    """
    names = _provider_names(providers)
    roles = task_roles(names)
    gateway_module = _harness_module("providers.gateway")
    docs_module = _harness_module("providers.official_docs")
    client = httpx.AsyncClient()  # required by both constructors; no request is ever made through it here
    access: dict[str, dict[str, object]] = {
        name: {"base_url": f"https://{name.replace('_', '-')}.twin.invalid", "env": dict[str, str]()} for name in names
    }
    gateway = gateway_module.ProviderGateway(
        access,
        provider_roles=roles,
        max_calls=provider_tool_limit(),
        candidate_safe_surface=True,
        client=client,
    )
    docs = docs_module.OfficialDocsGateway(names, provider_roles=roles, max_calls=docs_tool_limit(), client=client)
    return [
        _tool_definition(gateway.tool_definition, PROVIDER_API_TOOL),
        _tool_definition(docs.tool_definition, PROVIDER_DOCS_TOOL),
    ]


class DocsExecutor:
    """An `execute_tool`-shaped callable for `provider_docs`, backed by the harness `OfficialDocsGateway`.

    Both agents (stock baseline and Benchpress) receive the same instance shape, so documentation access is
    identical across the comparison. `search` is answered from the bundled catalog offline; `fetch` reads the
    allowlisted official documentation page over the public internet.
    """

    def __init__(self, gateway: Any) -> None:
        self._gateway = gateway

    @property
    def tool_name(self) -> str:
        return str(self._gateway.tool_definition["name"])

    @property
    def tool_definition(self) -> dict[str, Any]:
        return _tool_definition(self._gateway.tool_definition, PROVIDER_DOCS_TOOL)

    async def __call__(self, tool_name: str, tool_input: dict[str, Any]) -> object:
        if tool_name != self.tool_name:
            return {"ok": False, "error": f"unknown tool {tool_name!r}"}
        return await self._gateway.execute(tool_input)

    def trace_records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for record in self._gateway.trace_records:
            records.append(_mapping(record.to_dict(), "trace record"))
        return records

    async def aclose(self) -> None:
        await self._gateway.aclose()


_shared_docs_cache: Any = None


def _docs_snapshot_cache(docs_module: ModuleType) -> Any:
    """One `OfficialDocsSnapshotCache` per process, like the run script's `docs_cache` shared across tasks."""
    global _shared_docs_cache
    if _shared_docs_cache is None:
        _shared_docs_cache = docs_module.OfficialDocsSnapshotCache()
    return _shared_docs_cache


def docs_executor(
    providers: Sequence[str],
    *,
    max_calls: int | None = None,
    client: httpx.AsyncClient | None = None,
    shared_cache: bool = True,
) -> DocsExecutor | None:
    """A `provider_docs` executor for these providers, or None if the harness refuses to build one.

    The only refusal the harness can raise offline is a provider outside its documentation catalog
    (`SUPPORTED_DOC_PROVIDERS`); every provider Benchpress runs against (slack, gmail, hubspot, stripe,
    github, linear) is covered. The reason is logged. No Arga credentials are involved at any point.
    """
    docs_module = _harness_module("providers.official_docs")
    names = _provider_names(providers)
    try:
        gateway = docs_module.OfficialDocsGateway(
            names,
            provider_roles=task_roles(names),
            max_calls=docs_tool_limit() if max_calls is None else max_calls,
            snapshot_cache=_docs_snapshot_cache(docs_module) if shared_cache else None,
            client=client,
        )
    except docs_module.OfficialDocsConfigurationError as error:
        _log.warning("provider_docs unavailable for %s: %s", ", ".join(names), error)
        return None
    return DocsExecutor(gateway)
