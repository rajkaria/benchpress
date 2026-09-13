"""`benchpress` command line: run a request against real apps or local twins, print receipts."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

from benchpress.api import DEFAULT_SYSTEM_PROMPT
from benchpress.controller import run_trial
from benchpress.model import ModelConfig
from benchpress.phases.common import Ablations
from benchpress.receipt_html import write_receipt_html
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
    receipt = sub.add_parser("receipt", help="print a receipt summary")
    receipt.add_argument("path")
    receipt.add_argument(
        "--html",
        nargs="?",
        const="",
        metavar="OUT",
        help="also render the self-contained receipt page; defaults to receipt.html next to the JSON",
    )
    args = parser.parse_args(argv)
    if args.command == "run":
        return asyncio.run(_run(args))
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
