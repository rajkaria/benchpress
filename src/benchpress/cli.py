"""`benchpress` command line: run a request against real apps or local twins, print receipts."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

from benchpress.api import DEFAULT_SYSTEM_PROMPT
from benchpress.controller import run_trial
from benchpress.model import ModelConfig
from benchpress.phases.common import Ablations
from benchpress.playbooks import Playbook
from benchpress.receipt_html import write_receipt_html
from benchpress.rehearse import ModelFactory, Rehearsal, StageFactory, rehearse, replay
from benchpress.report import receipt_summary


class GatewayLike(Protocol):
    async def execute_tool(self, tool_name: str, tool_input: dict[str, Any]) -> object: ...

    async def aclose(self) -> None: ...


class GatewayFactory(Protocol):
    def __call__(self, providers: Sequence[str]) -> GatewayLike: ...


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - dotenv is a dev dependency
        return
    load_dotenv(Path.cwd() / ".env")


async def _run(args: argparse.Namespace) -> int:
    prompt = Path(args.prompt_file).read_text() if args.prompt_file else str(args.prompt or "")
    if not prompt.strip():
        print("a --prompt or --prompt-file is required", file=sys.stderr)
        return 2
    providers = [name.strip() for name in str(args.providers).split(",") if name.strip()]
    try:
        realapp = importlib.import_module("benchpress.realapp")
    except ImportError as exc:
        print(f"real-app gateway unavailable: {exc}", file=sys.stderr)
        return 2
    factory = cast(GatewayFactory, getattr(realapp, "gateway_from_env"))  # noqa: B009 - module resolved at runtime
    gateway = factory(providers)
    trace_dir = Path(args.trace_dir or f"runs/local/{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}")
    config = ModelConfig.from_env(model=args.model)
    result = await run_trial(
        system_prompt=DEFAULT_SYSTEM_PROMPT,
        user_prompt=prompt,
        providers=providers,
        execute_tool=gateway.execute_tool,
        config=config,
        ablations=Ablations.parse(args.ablations),
        trace_dir=trace_dir,
    )
    await gateway.aclose()
    receipt = json.loads((trace_dir / "receipt.json").read_text())
    print(receipt_summary(receipt))
    print(f"\nfinal:\n{result.final_text}")
    print(f"\nreceipt: {trace_dir / 'receipt.json'}")
    return 0


def _import_callable(spec: str, flag: str) -> Callable[..., Any]:
    module_name, _, attribute = spec.partition(":")
    if not module_name or not attribute:
        raise ValueError(f"{flag} expects module:callable, got {spec!r}")
    target = getattr(importlib.import_module(module_name), attribute, None)
    if not callable(target):
        raise ValueError(f"{flag} {spec!r} is not a module:callable")
    return cast(Callable[..., Any], target)


async def _rehearse(args: argparse.Namespace) -> int:
    prompt = Path(args.prompt_file).read_text() if args.prompt_file else str(args.prompt or "")
    if not prompt.strip():
        print("a --prompt or --prompt-file is required", file=sys.stderr)
        return 2
    providers = [name.strip() for name in str(args.providers).split(",") if name.strip()]
    try:
        stage_builder = _import_callable(args.stage, "--stage")
        model_factory: ModelFactory | None = None
        if args.model_factory:
            model_factory = cast(ModelFactory, _import_callable(args.model_factory, "--model-factory"))
        playbook_builder = _import_callable(args.playbooks, "--playbooks") if args.playbooks else None
    except (ValueError, ImportError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    stage_factory = cast(StageFactory, stage_builder(providers, args.seed))
    playbooks = cast(Mapping[str, Playbook], playbook_builder(providers)) if playbook_builder else None
    result = await rehearse(
        prompt,
        providers,
        stage_factory,
        n=args.n,
        system_prompt=DEFAULT_SYSTEM_PROMPT,
        config=None if model_factory is not None else ModelConfig.from_env(model=args.model),
        model_factory=model_factory,
        playbooks=playbooks,
        trace_dir=Path(args.trace_dir) if args.trace_dir else None,
    )
    out = result.write_json(Path(args.out))
    print(result.summary())
    print(f"\nrehearsal: {out}")
    return 0 if result.converged else 1


async def _never_execute(tool_name: str, tool_input: dict[str, Any]) -> object:
    raise RuntimeError("replay of a non-converged rehearsal must not call a provider")


async def _replay(args: argparse.Namespace) -> int:
    rehearsal = Rehearsal.read_json(Path(args.path))
    if not rehearsal.converged:
        receipt = await replay(rehearsal, _never_execute)
    else:
        providers = [name.strip() for name in str(args.providers or "").split(",") if name.strip()]
        realapp = importlib.import_module("benchpress.realapp")
        factory = cast(GatewayFactory, getattr(realapp, "gateway_from_env"))  # noqa: B009 - resolved at runtime
        gateway = factory(providers or list(rehearsal.providers))
        try:
            receipt = await replay(rehearsal, gateway.execute_tool)
        finally:
            await gateway.aclose()
    if args.out:
        receipt.write_json(Path(args.out))
    print(receipt.summary())
    return 0 if receipt.status == "completed" else 1


def main(argv: list[str] | None = None) -> int:
    _load_env()
    parser = argparse.ArgumentParser(
        prog="benchpress", description="the reliability layer for agents with write access"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run one request end to end")
    run.add_argument("--prompt")
    run.add_argument("--prompt-file")
    run.add_argument("--providers", default="slack,gmail,hubspot,stripe")
    run.add_argument("--trace-dir")
    run.add_argument("--model", default=os.environ.get("BENCHPRESS_MODEL"))
    run.add_argument("--ablations", default=os.environ.get("BENCHPRESS_ABLATIONS"))
    demo = sub.add_parser("demo", help="run the whole loop offline on an in-memory workspace (no keys, no network)")
    demo.add_argument("--trace-dir", default="benchpress-demo")
    receipt = sub.add_parser("receipt", help="print a receipt summary")
    receipt.add_argument("path")
    receipt.add_argument(
        "--html",
        nargs="?",
        const="",
        metavar="OUT",
        help="also render the self-contained receipt page; defaults to receipt.html next to the JSON",
    )
    guard = sub.add_parser("mcp-guard", help="MCP stdio proxy: writes are refused unless a policy rule allows them")
    guard.add_argument("--policy", required=True, help="guard policy JSON (see docs/MCP.md)")
    guard.add_argument("--receipts", help="JSONL receipt path; defaults to mcp-guard-receipts.jsonl next to the policy")
    guard.add_argument("upstream", nargs=argparse.REMAINDER, help="-- <upstream MCP server command...>")
    rehearsal = sub.add_parser("rehearse", help="run a request n times on fresh stages and check convergence")
    rehearsal.add_argument("--prompt")
    rehearsal.add_argument("--prompt-file")
    rehearsal.add_argument("--providers", default="slack,gmail,hubspot,stripe")
    rehearsal.add_argument("--n", type=int, default=3)
    rehearsal.add_argument("--out", default="rehearsal.json")
    rehearsal.add_argument(
        "--stage", required=True, help="module:callable taking (providers, seed_file) and returning a StageFactory"
    )
    rehearsal.add_argument("--seed", help="seed file handed to the --stage callable")
    rehearsal.add_argument("--model-factory", help="module:callable taking a run index and returning a ModelClient")
    rehearsal.add_argument("--playbooks", help="module:callable taking providers and returning playbooks")
    rehearsal.add_argument("--trace-dir")
    rehearsal.add_argument("--model", default=os.environ.get("BENCHPRESS_MODEL"))
    replay_cmd = sub.add_parser("replay", help="replay a converged rehearsal against gateway_from_env targets")
    replay_cmd.add_argument("path")
    replay_cmd.add_argument("--providers", help="override the rehearsal's providers")
    replay_cmd.add_argument("--out", help="write the replay receipt JSON here")
    gate = sub.add_parser("gate", help="check the mutation gate against the gate-rule corpus")
    gate_sub = gate.add_subparsers(dest="gate_command", required=True)
    gate_check = gate_sub.add_parser("check", help="run corpus cases; exit 1 on any mismatch")
    gate_check.add_argument("paths", nargs="*", metavar="PATH", help="case files or directories (default: bundled)")
    gate_check.add_argument("-v", "--verbose", action="store_true", help="print every reason and xfail note")
    policy = sub.add_parser("policy", help="list and inspect policy packs")
    policy_sub = policy.add_subparsers(dest="policy_command", required=True)
    policy_sub.add_parser("list", help="the bundled policy packs")
    policy_show = policy_sub.add_parser("show", help="the rules of one policy pack")
    policy_show.add_argument("name", help="bundled pack name, or path to a pack YAML file")
    args = parser.parse_args(argv)
    if args.command == "mcp-guard":
        try:
            from benchpress.shims import mcp_guard  # the mcp extra is optional; import only when asked
        except ImportError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        return mcp_guard.main(args.policy, args.upstream, args.receipts)
    if args.command == "policy":
        from benchpress.packs import list_command, show_command

        return list_command() if args.policy_command == "list" else show_command(str(args.name))
    if args.command == "gate":
        from benchpress.gate_corpus import check_command

        return check_command(list(args.paths), verbose=bool(args.verbose))
    if args.command == "run":
        return asyncio.run(_run(args))
    if args.command == "demo":
        from benchpress.demo import main as demo_main

        return demo_main(Path(args.trace_dir))
    if args.command == "rehearse":
        return asyncio.run(_rehearse(args))
    if args.command == "replay":
        return asyncio.run(_replay(args))
    if args.command == "receipt":
        path = Path(args.path)
        if path.is_dir():
            path = path / "receipt.json"
        print(receipt_summary(json.loads(path.read_text())))
        if args.html is not None:
            out = Path(args.html) if args.html else path.with_suffix(".html")
            print(f"\nreceipt page: {write_receipt_html(path, out)}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
