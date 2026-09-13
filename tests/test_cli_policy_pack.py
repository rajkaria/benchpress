"""`benchpress run --policy-pack` loads packs before any provider call and hands them to the gate."""

from __future__ import annotations

from typing import Any

import pytest

import benchpress.cli as cli
import benchpress.realapp as realapp


class _Captured(Exception):
    def __init__(self, kwargs: dict[str, Any]) -> None:
        super().__init__("captured")
        self.kwargs = kwargs


class _Gateway:
    async def execute_tool(self, tool_name: str, tool_input: dict[str, Any]) -> object:
        raise AssertionError("no provider call expected")

    async def aclose(self) -> None:
        return None


def test_unknown_policy_pack_is_a_usage_error_before_any_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_gateway(providers: object) -> _Gateway:
        raise AssertionError("gateway must not be built for a bad pack")

    monkeypatch.setattr(realapp, "gateway_from_env", no_gateway)
    assert cli.main(["run", "--prompt", "update the billing contact", "--policy-pack", "no-such-pack"]) == 2


def test_policy_packs_reach_run_trial(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    def fake_gateway(providers: object) -> _Gateway:
        return _Gateway()

    monkeypatch.setattr(realapp, "gateway_from_env", fake_gateway)

    async def capture(**kwargs: Any) -> None:
        raise _Captured(kwargs)

    monkeypatch.setattr(cli, "run_trial", capture)
    argv = ["run", "--prompt", "update the billing contact", "--providers", "stripe"]
    argv += ["--policy-pack", "billing", "--policy-pack", "customer-success", "--trace-dir", str(tmp_path)]
    with pytest.raises(_Captured) as caught:
        cli.main(argv)
    packs = caught.value.kwargs["policy_packs"]
    assert len(packs) == 2
