"""`evals/harness_bridge.py` borrows from the vendored ArgaBench harness byte for byte.

These tests import the harness themselves (module and run script) and compare, so any drift in what the
judges' harness sends to a candidate fails here first.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import httpx
import pytest

from evals import harness_bridge
from evals.harness_bridge import HarnessNotFoundError, TaskSpec

# Every test here imports or compares against the vendored ArgaBench harness.
pytestmark = pytest.mark.arga


def _load_run_script() -> ModuleType:
    """Import `scripts/run_argabench_40.py` as a module (registered in sys.modules so dataclasses resolve)."""
    harness_bridge.ensure_harness_importable()
    path = harness_bridge.harness_root() / harness_bridge.RUN_SCRIPT_PATH
    name = "argabench_run_script_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _suite_task(task_id: str) -> dict[str, object]:
    payload = json.loads((harness_bridge.harness_root() / harness_bridge.SUITE_PATH).read_text())
    return next(task for task in payload["tasks"] if task["id"] == task_id)


def test_system_prompt_is_the_harness_constant() -> None:
    harness_bridge.ensure_harness_importable()
    prompting = importlib.import_module("arga_twins_benchmark.runner.prompting")
    assert harness_bridge.system_prompt() == prompting.SYSTEM_PROMPT
    assert harness_bridge.system_prompt() != prompting.LEGACY_SYSTEM_PROMPT


def test_user_prompt_is_the_raw_suite_prompt_exactly_as_the_run_script_sends_it() -> None:
    prompt = harness_bridge.user_prompt("ECOM-02")
    assert prompt == _suite_task("ECOM-02")["prompt"]
    assert prompt.startswith("Marlon Price from customer success just posted in the #commerce-ops Slack channel")

    # The composition rule, verified against the script source: `invoke_model(... user_prompt=task["prompt"] ...)`,
    # and no call to `compose_user_prompt` anywhere in the suite runner.
    source = (harness_bridge.harness_root() / harness_bridge.RUN_SCRIPT_PATH).read_text()
    invoke_call = re.search(r"invocation = await invoke_model\((.*?)\)\n", source, re.DOTALL)
    assert invoke_call is not None
    assert 'user_prompt=task["prompt"]' in invoke_call.group(1)
    assert "system_prompt=SYSTEM_PROMPT" in invoke_call.group(1)
    assert "compose_user_prompt" not in source
    assert "structured_output_instruction" not in source


def test_provider_roles_and_limits_match_the_run_script() -> None:
    script = _load_run_script()
    assert harness_bridge.provider_roles() == script.PROVIDER_ROLES
    assert harness_bridge.provider_tool_limit() == script.PROVIDER_TOOL_LIMIT == 160
    assert harness_bridge.docs_tool_limit() == script.OFFICIAL_DOCS_TOOL_LIMIT == 40
    assert harness_bridge.model_timeout_seconds() == script.MODEL_TIMEOUT_SECONDS == 1800
    task = _suite_task("ECOM-02")
    assert harness_bridge.task_roles(list(task["twins"])) == script.task_roles(task)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="without a harness role"):
        harness_bridge.task_roles(["slack", "carrier-pigeon"])


def test_tool_schema_matches_the_harness_gateways() -> None:
    tools = harness_bridge.tool_schema(["slack", "stripe"])
    assert [tool["name"] for tool in tools] == ["provider_api", "provider_docs"]
    api_enum = tools[0]["input_schema"]["properties"]["provider"]["enum"]
    docs_enum = tools[1]["input_schema"]["properties"]["provider"]["enum"]
    assert api_enum == docs_enum == ["payments", "slack", "stripe", "team_chat"]
    assert tools[0]["input_schema"]["properties"]["method"]["enum"] == ["DELETE", "GET", "PATCH", "POST", "PUT"]
    assert tools[0]["input_schema"]["required"] == ["provider", "method", "path"]
    assert tools[1]["input_schema"]["required"] == ["provider", "action"]

    # Independently built through the harness classes with the script's own role table.
    script = _load_run_script()
    gateway_module = importlib.import_module("arga_twins_benchmark.providers.gateway")
    docs_module = importlib.import_module("arga_twins_benchmark.providers.official_docs")
    roles = script.task_roles({"twins": ["slack", "stripe"]})
    client = httpx.AsyncClient()
    access: dict[str, dict[str, object]] = {
        name: {"base_url": "https://twin.invalid", "env": dict[str, str]()} for name in ("slack", "stripe")
    }
    expected_api = gateway_module.ProviderGateway(access, provider_roles=roles, max_calls=160, client=client)
    expected_docs = docs_module.OfficialDocsGateway(
        ["slack", "stripe"], provider_roles=roles, max_calls=40, client=client
    )
    assert tools[0] == expected_api.tool_definition
    assert tools[1] == expected_docs.tool_definition


def test_tool_schema_for_every_ecom_02_twin() -> None:
    tools = harness_bridge.tool_schema(harness_bridge.task_spec("ECOM-02").twins)
    enum = tools[0]["input_schema"]["properties"]["provider"]["enum"]
    assert enum == ["email", "gmail", "hubspot", "hubspot_crm", "payments", "slack", "stripe", "team_chat"]


def test_task_spec_fields() -> None:
    spec = harness_bridge.task_spec("ECOM-02")
    assert isinstance(spec, TaskSpec)
    assert spec.task_id == "ECOM-02"
    assert spec.title == "Billing contact change reconciliation"
    assert spec.domain == "ecommerce"
    assert spec.twins == ("gmail", "hubspot", "slack", "stripe")
    assert spec.prompt == harness_bridge.user_prompt("ECOM-02")
    assert spec.facts == {
        "customer": "Northwind Studio",
        "former_contact": "billing@northwindstudio.example",
        "verified_contact": "ap@northwindstudio.example",
    }
    assert spec.protected_terms == ("Northwind Studios Prospect", "northwind-studios.example")
    assert spec.allowed_providers == ("stripe", "slack", "hubspot", "gmail")
    assert spec.minimum_semantic_steps == 8
    assert set(spec.seed_config) == {"gmail", "hubspot", "slack", "stripe"}
    assert [outcome["id"] for outcome in spec.required_outcomes] == [
        "primary_outcome",
        "cross_system_correlation",
        "originating_channel_update",
        "structured_result",
        "reviewed_unsent_confirmation",
    ]
    assert [outcome["id"] for outcome in spec.forbidden_outcomes] == [
        "protected_candidate_mutation",
        "duplicate_business_resource",
        "collateral_damage",
        "control_plane_access",
    ]


def test_task_spec_stringifies_integer_facts_and_covers_the_whole_suite() -> None:
    assert len(harness_bridge.all_task_ids()) == 40
    crm_05 = harness_bridge.task_spec("CRM-05")
    assert crm_05.facts["eligible_contacts"] == "29"
    for task_id in harness_bridge.all_task_ids():
        spec = harness_bridge.task_spec(task_id)
        assert spec.twins and spec.prompt and spec.facts
        assert (
            set(spec.allowed_providers) <= set(spec.twins) | {"jira", "salesforce", "linear"} or spec.allowed_providers
        )
    with pytest.raises(ValueError, match="unknown ArgaBench task"):
        harness_bridge.task_spec("ECOM-99")


def test_task_spec_returns_copies() -> None:
    first = harness_bridge.task_spec("ECOM-02")
    first.seed_config["gmail"]["messages"].clear()
    first.facts["customer"] = "changed"
    second = harness_bridge.task_spec("ECOM-02")
    assert len(second.seed_config["gmail"]["messages"]) == 5
    assert second.facts["customer"] == "Northwind Studio"


@pytest.mark.asyncio
async def test_docs_executor_search_works_offline_without_arga() -> None:
    def no_network(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected network request {request.method} {request.url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(no_network))
    executor = harness_bridge.docs_executor(["gmail", "slack"], client=client, shared_cache=False)
    assert executor is not None
    assert executor.tool_name == "provider_docs"
    assert executor.tool_definition == harness_bridge.tool_schema(["gmail", "slack"])[1]

    raw = await executor("provider_docs", {"provider": "email", "action": "search", "query": "drafts"})
    assert isinstance(raw, dict)
    result = cast(dict[str, Any], raw)
    assert result["ok"] is True
    assert result["provider"] == "gmail"
    assert result["requested_provider"] == "email"
    documents = cast(list[dict[str, Any]], result["documents"])
    assert documents, "the bundled catalog answers search offline"
    assert all(str(doc["source_url"]).startswith("https://developers.google.com/") for doc in documents)
    assert {doc["id"] for doc in documents} >= {"rest-reference", "drafts-resource"}

    unknown = await executor("provider_api", {"provider": "gmail", "method": "GET", "path": "/x"})
    assert unknown == {"ok": False, "error": "unknown tool 'provider_api'"}
    assert len(executor.trace_records()) == 1
    assert executor.trace_records()[0]["action"] == "search"
    await executor.aclose()
    await client.aclose()


def test_harness_root_error_names_the_fix(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(harness_bridge.HARNESS_ROOT_ENV, str(tmp_path))
    with pytest.raises(HarnessNotFoundError, match="ln -sfn"):
        harness_bridge.harness_root()
    monkeypatch.delenv(harness_bridge.HARNESS_ROOT_ENV)
    assert (harness_bridge.harness_root() / harness_bridge.SUITE_PATH).is_file()


def test_ensure_harness_importable_is_idempotent() -> None:
    harness_bridge.ensure_harness_importable()
    before = list(sys.path)
    harness_bridge.ensure_harness_importable()
    assert sys.path == before
    resolved = {str(Path(entry).resolve()) for entry in sys.path if entry.endswith("arga-twins-benchmark/src")}
    assert len(resolved) == 1
