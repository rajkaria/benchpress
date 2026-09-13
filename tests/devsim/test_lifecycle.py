"""FakeArgaCli satisfies the harness's ArgaCli/TwinRun/state-capture contracts (TRACK-DEVSIM §1)."""

from __future__ import annotations

from typing import Any, cast

import httpx
import pytest

from devsim.candidates import stub
from devsim.harness import SUITE_TAG, content_hash, harness_module, suite_task
from devsim.lifecycle import DevsimLifecycleError, FakeArgaCli, scenario_id_for
from devsim.runner import load_run_script, patch_run_script

TASK_ID = "ECOM-02"


async def test_catalog_lists_40_tagged_scenarios_resolvable_by_the_harness() -> None:
    cli = FakeArgaCli()
    scenarios = await cli.list_scenarios(tag=SUITE_TAG)
    assert len(scenarios) == 40
    task = suite_task(TASK_ID)
    digest = content_hash(task)
    matching = [item for item in scenarios if f"content-sha256:{digest}" in item["tags"]]
    assert len(matching) == 1 and matching[0]["description"] == task["prompt"]
    assert matching[0]["id"] == scenario_id_for(matching[0]) == cli.scenario_for_digest(digest)["id"]

    module = load_run_script()
    patched = patch_run_script(module, cli=cli, invoke_model=stub())
    try:
        resolved = await module.resolve_scenarios([task])  # the harness's own resolver, unmodified
    finally:
        patched.restore()
    assert resolved == {TASK_ID: matching[0]["id"]}


async def test_create_status_teardown_follow_the_twin_run_contract() -> None:
    cli = FakeArgaCli()
    task = suite_task(TASK_ID)
    scenario_id = cli.scenario_for_digest(content_hash(task))["id"]
    run = await cli.create_twin_run(twins=list(task["twins"]), scenario_id=scenario_id, ttl_minutes=60)
    try:
        assert run.is_public is True and run.status == "ready"
        assert set(run.twins) == set(task["twins"])
        for name, twin in run.twins.items():
            assert twin.base_url.startswith("http://127.0.0.1:")
            assert twin.admin_url is not None and twin.admin_url != twin.base_url
            assert twin.env_vars, name
            assert not any("ADMIN" in key or key.startswith("ARGA_") for key in twin.env_vars)
        access = run.candidate_access()
        assert access["stripe"]["env"] == {"STRIPE_API_KEY": run.twins["stripe"].env_vars["STRIPE_API_KEY"]}
        assert run.raw["substrate"] == "devsim-local-twins"

        # The harness's own control-payload → verifier-target resolution accepts it.
        module = load_run_script()
        control = module.control_payload(TASK_ID, scenario_id, content_hash(task), run)
        state_capture = harness_module("arga_twins_benchmark.evaluation.state_capture")
        targets = state_capture.targets_from_control(control, roles=module.task_roles(task))
        assert set(targets) == set(task["twins"])

        # The seeded stores are reachable for golden trajectories.
        local = cli.runs[run.run_id]
        assert set(local.stores) == set(task["twins"])
        assert local.seed_key == content_hash(task)

        status = await cli.status(run.run_id)
        assert status.run_id == run.run_id and status.status == "ready"
        admin_url = run.twins["slack"].admin_url
        assert admin_url is not None
        async with httpx.AsyncClient() as client:
            assert (await client.get(f"{admin_url}/admin/state")).status_code == 200
    finally:
        teardown = await cli.teardown(run.run_id)
    assert teardown["status"] == "torn_down" and teardown["twins"] == {}
    after = await cli.status(run.run_id)
    assert after.status == "torn_down" and after.twins == {}
    lifecycle = harness_module("arga_twins_benchmark.lifecycle")
    assert lifecycle.cleanup_payload_proves_inert({"twin_run": dict(after.raw)}, expected_run_id=run.run_id)
    with pytest.raises(httpx.TransportError):
        async with httpx.AsyncClient(timeout=1.0) as client:
            await client.get(f"{admin_url}/admin/state")
    # Idempotent teardown.
    assert (await cli.teardown(run.run_id))["twins"] == {}


async def test_harness_wait_cleanup_returns_inert_payload_immediately() -> None:
    cli = FakeArgaCli()
    task = suite_task(TASK_ID)
    scenario_id = cli.scenario_for_digest(content_hash(task))["id"]
    run = await cli.create_twin_run(twins=["slack"], scenario_id=scenario_id, ttl_minutes=5)
    module = load_run_script()
    cleanup = cast(dict[str, Any], await module.wait_cleanup(cli, run.run_id))
    assert cleanup["confirmation"] == {"outcome": "terminal_without_twins", "confirmed_status": "torn_down"}
    assert cleanup["teardown"]["outcome"] == "accepted"
    lifecycle = harness_module("arga_twins_benchmark.lifecycle")
    assert lifecycle.cleanup_payload_proves_inert(cleanup, expected_run_id=run.run_id)


async def test_reset_reprovisions_under_the_same_run_id() -> None:
    cli = FakeArgaCli()
    task = suite_task(TASK_ID)
    scenario_id = cli.scenario_for_digest(content_hash(task))["id"]
    run = await cli.create_twin_run(twins=["stripe"], scenario_id=scenario_id, ttl_minutes=5)
    try:
        before = cli.runs[run.run_id].store("stripe")
        payload = await cli.reset(run.run_id)
        assert payload["status"] == "ready" and payload["run_id"] == run.run_id
        assert cli.runs[run.run_id].store("stripe") is not before
    finally:
        await cli.stop_all()


async def test_unknown_ids_raise_lookup_errors_not_arga_cli_errors() -> None:
    cli = FakeArgaCli()
    with pytest.raises(DevsimLifecycleError):
        await cli.status("nope")
    with pytest.raises(DevsimLifecycleError):
        await cli.create_twin_run(twins=["slack"], scenario_id="nope", ttl_minutes=1)
    assert issubclass(DevsimLifecycleError, LookupError)
