"""`benchpress rehearse` / `benchpress replay`, exercised only against in-memory stages.

The stage, model and playbooks are injected through the CLI's `module:callable` plug points; the
replay gateway is monkeypatched. Nothing here reads `.env` or opens a socket.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

import benchpress.cli as cli
import benchpress.realapp as realapp
from benchpress.demo import PROMPT, SCRIPT
from benchpress.model import ModelClient
from benchpress.playbooks import Playbook
from benchpress.rehearse import Rehearsal, StageFactory
from tests.test_rehearse import (
    OffsetWorkspace,
    WorkspaceStage,
    convergent_script,
    model_factory,
    playbooks,
    workspace_factory,
)

PROVIDERS = "slack,gmail,hubspot,stripe"


def stage_builder(providers: Sequence[str], seed_file: str | None) -> StageFactory:
    assert list(providers) == PROVIDERS.split(",")
    return workspace_factory()


def scripted_model(index: int) -> ModelClient:
    return model_factory([convergent_script()])(index)


def refusing_model(index: int) -> ModelClient:
    return model_factory([SCRIPT])(index)


def fake_playbooks(providers: Sequence[str]) -> dict[str, Playbook]:
    return playbooks()


class FakeGateway:
    def __init__(self, stage: WorkspaceStage) -> None:
        self.stage = stage
        self.closed = False

    async def execute_tool(self, tool_name: str, tool_input: dict[str, Any]) -> object:
        return await self.stage.execute_tool(tool_name, tool_input)

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_load_env", lambda: None)


def rehearse_args(out: Path, model: str) -> list[str]:
    return [
        "rehearse",
        "--providers",
        PROVIDERS,
        "--prompt",
        PROMPT,
        "--n",
        "2",
        "--stage",
        "tests.test_rehearse_cli:stage_builder",
        "--model-factory",
        f"tests.test_rehearse_cli:{model}",
        "--playbooks",
        "tests.test_rehearse_cli:fake_playbooks",
        "--out",
        str(out),
    ]


def test_rehearse_then_replay_through_the_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "rehearsal.json"
    assert cli.main(rehearse_args(out, "scripted_model")) == 0
    rehearsal = Rehearsal.read_json(out)
    assert rehearsal.converged and rehearsal.n == 2 and rehearsal.plan
    assert "CONVERGED" in capsys.readouterr().out

    target = WorkspaceStage(OffsetWorkspace())
    gateways: list[FakeGateway] = []

    def fake_gateway_from_env(providers: Sequence[str]) -> FakeGateway:
        assert list(providers) == PROVIDERS.split(",")
        gateways.append(FakeGateway(target))
        return gateways[-1]

    monkeypatch.setattr(realapp, "gateway_from_env", fake_gateway_from_env)
    receipt_path = tmp_path / "replay.json"
    assert cli.main(["replay", str(out), "--out", str(receipt_path)]) == 0
    receipt = json.loads(receipt_path.read_text())
    assert receipt["status"] == "completed" and receipt["executed"] == len(rehearsal.plan)
    assert gateways and gateways[0].closed
    assert target.workspace.customers["cus_R1"]["email"] == "ap@rivermill.example"
    assert "COMPLETED" in capsys.readouterr().out


def test_a_diverged_rehearsal_exits_nonzero_and_replay_never_builds_a_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "rehearsal.json"
    assert cli.main(rehearse_args(out, "refusing_model")) == 1
    assert "DIVERGED" in capsys.readouterr().out

    def no_gateway(providers: Sequence[str]) -> FakeGateway:
        raise AssertionError("a non-converged rehearsal must not reach any provider")

    monkeypatch.setattr(realapp, "gateway_from_env", no_gateway)
    assert cli.main(["replay", str(out)]) == 1
    assert "REFUSED" in capsys.readouterr().out


def test_rehearse_rejects_a_malformed_stage_spec(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args = rehearse_args(tmp_path / "r.json", "scripted_model")
    args[args.index("--stage") + 1] = "not-a-callable-spec"
    assert cli.main(args) == 2
    assert "module:callable" in capsys.readouterr().err
