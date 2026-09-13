"""D0 scaffold: port blocks, in-process servers, output-root configs, matrix copies, run_task end to end."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from devsim.candidates import stub
from devsim.harness import SUITE_TAG, harness_module, historical_calibration_path, suite_path
from devsim.lifecycle import FakeArgaCli
from devsim.matrix import (
    DEFAULT_SWAP_SLOT,
    EXPECTED_PROFILE_COUNT,
    ProfileError,
    canonical_profiles,
    load_profile,
    matrix_profiles,
    write_matrix_copy,
)
from devsim.runner import (
    SWAPPED_GLOBALS,
    TRIAL_ARTIFACTS,
    load_run_script,
    patch_run_script,
    prepare_output_root,
    run_trial,
    trial_issues,
)
from devsim.server import PortBlock, TwinServer, is_stub_spec, serve_twins, spec_for
from devsim.twins.stub import make_stub_spec

TASK_ID = "ECOM-02"


def test_port_block_allocates_distinct_listening_ports() -> None:
    block = PortBlock.allocate(4)
    try:
        ports = block.ports
        assert len(ports) == 4 and len(set(ports)) == 4
        assert all(port > 0 for port in ports)
        first = block.take()
        assert first.getsockname()[1] == ports[0]
        first.close()
    finally:
        block.close()


async def test_twin_server_start_stop_with_health() -> None:
    spec = make_stub_spec("stripe", "payments")
    store = spec.make_store("seed")
    block = PortBlock.allocate(1)
    server = TwinServer(spec.make_admin_app(store), block.take(), label="stripe-admin")
    await server.start()
    try:
        async with httpx.AsyncClient() as client:
            health = await client.get(f"{server.url}/healthz")
            assert health.status_code == 200 and health.json()["provider"] == "stripe"
    finally:
        await server.stop()
        block.close()
    with pytest.raises(httpx.TransportError):
        async with httpx.AsyncClient(timeout=1.0) as client:
            await client.get(f"{server.url}/healthz")


async def test_serve_twins_falls_back_to_stubs_and_serves_admin_state() -> None:
    # jira and notion have no twin module, so they must fall back to stubs.
    running = await serve_twins(["notion", "jira"], {"jira": {"issues": [{"a": 1}]}}, "seed-key")
    try:
        assert running.providers == ("jira", "notion")
        assert all(twin.is_stub for twin in running.twins.values())
        assert running["jira"].role == "jira_tracker"
        assert running["jira"].base_url != running["jira"].admin_url
        async with httpx.AsyncClient() as client:
            data = await client.get(f"{running['jira'].base_url}/rest/api/3/issue/X-1")
            assert data.status_code == 404 and data.json() == {"error": "devsim stub: no route"}
            admin = await client.get(f"{running['jira'].admin_url}/admin/state")
            assert admin.status_code == 200 and isinstance(admin.json(), dict)
        env = running.env_lines()
        assert any(line.startswith("DEVSIM_JIRA_URL=http://127.0.0.1:") for line in env)
    finally:
        await running.stop()


def test_spec_for_uses_stub_only_for_missing_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    fallback = spec_for("nonexistent_provider")
    assert is_stub_spec(fallback) and fallback.provider == "nonexistent_provider"

    def broken_twin(provider: str) -> object:
        # A real twin module whose *own* import fails must surface, never become a stub.
        raise ModuleNotFoundError("No module named 'pydantic_extra'", name="pydantic_extra")

    monkeypatch.setattr("devsim.server.load_spec", broken_twin)
    with pytest.raises(ModuleNotFoundError):
        spec_for("stripe")


def test_load_profile_reads_canonical_and_devsim_profiles() -> None:
    canonical = load_profile("opus-5-high")
    assert canonical["model_id"] == "claude-opus-5"
    devsim = load_profile("baseline-deepseek-chat")
    assert devsim["provider"] == "openai" and devsim["agent"] == "baseline"
    assert load_profile("benchpress-opus-5-high")["model_id"] == "claude-opus-5"
    with pytest.raises(ProfileError):
        load_profile("no-such-profile")


def test_write_matrix_copy_has_37_profiles_with_one_slot_swapped(tmp_path: Path) -> None:
    profile = load_profile("benchpress-deepseek-chat")
    path = write_matrix_copy(tmp_path / "model-matrix.json", swap_out=DEFAULT_SWAP_SLOT, swap_in=profile)
    profiles = matrix_profiles(path)
    ids = [str(item["id"]) for item in profiles]
    assert len(profiles) == EXPECTED_PROFILE_COUNT
    assert "benchpress-deepseek-chat" in ids and DEFAULT_SWAP_SLOT not in ids
    canonical_ids = [str(item["id"]) for item in canonical_profiles()]
    assert ids.index("benchpress-deepseek-chat") == canonical_ids.index(DEFAULT_SWAP_SLOT)
    payload = json.loads(path.read_text())
    assert payload["devsim_provenance"]["swapped"] == [
        {"slot": DEFAULT_SWAP_SLOT, "profile_id": "benchpress-deepseek-chat"}
    ]


def test_prepare_output_root_writes_classifier_configs(tmp_path: Path) -> None:
    output = tmp_path / "matrix"
    prepared = prepare_output_root(output, load_profile("baseline-deepseek-chat"), [TASK_ID])
    matrix_config = json.loads(prepared.matrix_config_path.read_text())
    assert matrix_config["protocol"] == "argabench-model-matrix-run/1"
    assert matrix_config["profile_count"] == EXPECTED_PROFILE_COUNT == len(matrix_config["profiles"])
    assert matrix_config["task_ids"] == [TASK_ID]
    assert matrix_config["scenarios_per_profile"] == 1 and matrix_config["total_trials"] == EXPECTED_PROFILE_COUNT
    assert matrix_config["attempts_per_model_scenario_pair"] == 1
    run_config = json.loads(prepared.run_config_path.read_text())
    assert run_config["protocol"] == "argabench-run/2"
    assert isinstance(run_config["environment"], str) and run_config["environment"]
    assert 1 <= run_config["concurrency"] <= 16
    assert run_config["profile"]["id"] == "baseline-deepseek-chat"
    staging = json.loads(prepared.staging_scenarios_path.read_text())
    assert staging["suite_tag"] == SUITE_TAG
    assert staging["scenario_ids"][TASK_ID].startswith("devsim-ecom-02-")
    assert {str(item["id"]) for item in matrix_profiles(prepared.model_matrix_path)} == {
        str(item["id"]) for item in matrix_config["profiles"]
    }
    assert os.readlink(output / "tasks") == "profiles/baseline-deepseek-chat/tasks"

    # A second profile in the same matrix directory merges instead of clobbering.
    again = prepare_output_root(output, load_profile("benchpress-deepseek-chat"), [TASK_ID])
    merged = json.loads(again.matrix_config_path.read_text())
    ids = {str(item["id"]) for item in merged["profiles"]}
    assert {"baseline-deepseek-chat", "benchpress-deepseek-chat"} <= ids and len(ids) == EXPECTED_PROFILE_COUNT
    assert (output / "profiles" / "baseline-deepseek-chat" / "run-config.json").is_file()
    assert os.readlink(output / "tasks") == "profiles/benchpress-deepseek-chat/tasks"


def test_patch_run_script_swaps_only_the_documented_globals() -> None:
    module = load_run_script()
    originals = {name: getattr(module, name) for name in SWAPPED_GLOBALS}
    cli = FakeArgaCli()
    candidate = stub()
    patched = patch_run_script(module, cli=cli, invoke_model=candidate)
    try:
        assert patched.names == SWAPPED_GLOBALS
        assert module.SubprocessArgaCli() is cli
        assert module.invoke_model is candidate
        assert module.load_profile("baseline-deepseek-chat")["id"] == "baseline-deepseek-chat"
        # Everything else is the harness's own code.
        assert module.wait_ready is originals.get("wait_ready", module.wait_ready)
        assert module.run_task.__module__ == module.__name__
    finally:
        patched.restore()
    for name, value in originals.items():
        assert getattr(module, name) is value


async def test_run_trial_on_stubs_produces_every_artifact_and_classifies_exact_completed(tmp_path: Path) -> None:
    output = tmp_path / "d0"
    profile = load_profile("baseline-deepseek-chat")
    cli = FakeArgaCli()
    trial_dir = await run_trial(output, TASK_ID, profile, stub(), cli=cli)
    assert trial_dir == output / "profiles" / "baseline-deepseek-chat" / "tasks" / TASK_ID
    assert sorted(name for name in TRIAL_ARTIFACTS if (trial_dir / name).is_file()) == sorted(TRIAL_ARTIFACTS)
    assert trial_issues(trial_dir) == []  # both snapshots load via TrustedStateSnapshot.from_artifact_payload
    attempt = json.loads((trial_dir / "attempt.json").read_text())
    assert attempt["attempt_status"] == "candidate_complete"
    assert attempt["model_status"] == "completed"
    assert attempt["cleanup_succeeded"] is True
    assert attempt["response_model"] == profile["model_id"]
    control = json.loads((trial_dir / "control.json").read_text())
    assert control["twin_run"]["status"] == "ready" and control["twin_run"]["is_public"] is True
    assert set(control["twin_run"]["twins"]) == {"gmail", "hubspot", "slack", "stripe"}
    cleanup = json.loads((trial_dir / "cleanup.json").read_text())
    assert cleanup["twin_run"]["twins"] == {} and cleanup["confirmation"]["outcome"] == "terminal_without_twins"
    # Twins are torn down after the trial.
    run = cli.runs[attempt["run_id"]]
    assert run.status == "torn_down" and run.running is None

    classifier = harness_module("arga_twins_benchmark.reporting.argabench_matrix")
    classification = cast(
        dict[str, Any],
        classifier.classify_argabench_matrix(
            output,
            suite_path=suite_path(),
            model_matrix_path=output / "model-matrix.json",
            historical_calibration_path=historical_calibration_path(),
        ),
    )
    assert classification["matrix_integrity_issues"] == []
    ours = [
        item
        for item in cast(list[dict[str, Any]], classification["attempts"])
        if item["profile_id"] == "baseline-deepseek-chat"
    ]
    assert len(ours) == 1
    assert ours[0]["execution_class"] == "exact_completed", ours[0]["integrity"]["issues"]
    assert ours[0]["integrity"]["passed"] is True
