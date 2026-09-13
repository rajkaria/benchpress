"""Seed, snapshot, reset and verify the real apps for one published scenario.

    python -m evals.seed --task ECOM-02 --apps slack,stripe [--manifest runs/seed-manifest.json]
    python -m evals.seed --task ECOM-02 --apps slack,stripe --reset --verify
    python -m evals.seed --scenario billing-review --apps slack,stripe --snapshot runs/state.json

The seed config comes from the vendored ArgaBench scenario file (`--task`) or, when the
`evals.scenarios` registry is importable, from `evals.scenarios.load(<id>).seed_config`
(`--scenario`). Per-app counts are printed against the seed config's own counts, and
`--reset --verify` prints `clean` (exit 0) or one `residue:` line per leftover (exit 1).
`.env` is loaded first; scratch guards (`BENCHPRESS_SCRATCH_OK=1`, `sk_test_` keys) apply.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from dotenv import load_dotenv

from evals.realapps.base import RealApp, ScratchGuardError, SeedManifest, SeedResult

REPO_ROOT = Path(__file__).resolve().parents[1]
SCENARIO_DIR = REPO_ROOT / "arga-twins-benchmark" / "benchmark" / "argabench_40" / "scenarios"
DEFAULT_MANIFEST = REPO_ROOT / "runs" / "seed-manifest.json"
KNOWN_APPS: tuple[str, ...] = ("slack", "stripe", "hubspot", "gmail")
_DESCRIPTION = "Seed, snapshot, reset and verify the real apps for one published scenario."


# --------------------------------------------------------------------------------------
# Seed config
# --------------------------------------------------------------------------------------


def load_task_seed_config(task_id: str, scenario_dir: Path | None = None) -> tuple[str, dict[str, Any]]:
    """`(scenario_id, seed_config)` from the vendored scenario JSON, e.g. `ECOM-02` -> `ecom-02.json`."""
    path = (scenario_dir or SCENARIO_DIR) / f"{task_id.strip().lower()}.json"
    if not path.exists():
        raise FileNotFoundError(f"scenario file not found: {path} (is the arga-twins-benchmark symlink present?)")
    data = cast(dict[str, Any], json.loads(path.read_text()))
    seed_config = data.get("seed_config")
    if not isinstance(seed_config, Mapping):
        raise ValueError(f"{path} has no seed_config object")
    return task_id.strip().upper(), dict(cast(Mapping[str, Any], seed_config))


def load_registry_seed_config(scenario_id: str) -> tuple[str, dict[str, Any]] | None:
    """`evals.scenarios.load(id).seed_config` when that module exists (written in parallel)."""
    try:
        registry = importlib.import_module("evals.scenarios")
    except ImportError:
        return None
    loader = cast(Callable[[str], object] | None, getattr(registry, "load", None))
    if loader is None:
        return None
    scenario = loader(scenario_id)
    seed_config = getattr(scenario, "seed_config", None)
    if not isinstance(seed_config, Mapping):
        raise ValueError(f"evals.scenarios.load({scenario_id!r}) returned no seed_config mapping")
    return scenario_id, dict(cast(Mapping[str, Any], seed_config))


def resolve_seed_config(*, task: str | None, scenario: str | None) -> tuple[str, dict[str, Any]]:
    if scenario:
        loaded = load_registry_seed_config(scenario)
        if loaded is not None:
            return loaded
        return load_task_seed_config(scenario)
    if task:
        return load_task_seed_config(task)
    raise ValueError("one of --task or --scenario is required")


def expected_counts(seed_config: Mapping[str, Any]) -> dict[str, dict[str, int]]:
    """What a faithful seed must produce: every list in each provider's section, plus Slack messages."""
    expected: dict[str, dict[str, int]] = {}
    for app, section in seed_config.items():
        if not isinstance(section, Mapping):
            continue
        counts: dict[str, int] = {}
        for key, value in cast(Mapping[str, Any], section).items():
            if isinstance(value, list):
                counts[key] = len(cast(list[object], value))
        if app == "slack":
            channels = cast(Mapping[str, Any], section).get("channels")
            if isinstance(channels, list):
                counts["messages"] = sum(
                    len(cast(list[object], cast(Mapping[str, Any], channel).get("messages", [])))
                    for channel in cast(list[object], channels)
                    if isinstance(channel, Mapping)
                )
        expected[app] = counts
    return expected


# --------------------------------------------------------------------------------------
# Apps
# --------------------------------------------------------------------------------------


def build_apps(names: Sequence[str], env: Mapping[str, str]) -> tuple[dict[str, RealApp], list[str]]:
    """Instantiate drivers by name. Apps whose module or `from_env` factory is missing are skipped."""
    apps: dict[str, RealApp] = {}
    skipped: list[str] = []
    for raw_name in names:
        name = raw_name.strip().lower()
        if not name:
            continue
        try:
            module = importlib.import_module(f"evals.realapps.{name}")
        except ImportError:
            skipped.append(f"{name} (no evals.realapps.{name} module)")
            continue
        factory = cast(Callable[[Mapping[str, str]], RealApp] | None, getattr(module, "from_env", None))
        if factory is None:
            skipped.append(f"{name} (module has no from_env(env) factory)")
            continue
        apps[name] = factory(env)
    return apps, skipped


def format_counts(result: SeedResult, expected: Mapping[str, int]) -> tuple[str, bool]:
    parts: list[str] = []
    ok = True
    for key, actual in result.counts.items():
        want = expected.get(key)
        if want is None:
            parts.append(f"{key}={actual}")
            continue
        parts.append(f"{key}={actual}/{want}")
        ok = ok and actual == want
    return " ".join(parts), ok


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m evals.seed", description=_DESCRIPTION)
    parser.add_argument("--task", help="ArgaBench task id, e.g. ECOM-02 (reads the vendored scenario JSON)")
    parser.add_argument("--scenario", help="evals.scenarios id (falls back to the task file when unavailable)")
    parser.add_argument("--apps", default="slack,stripe", help=f"comma-separated subset of {','.join(KNOWN_APPS)}")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST), help="seed manifest path")
    parser.add_argument("--reset", action="store_true", help="reset from the manifest instead of seeding")
    parser.add_argument("--verify", action="store_true", help="verify the apps are clean (after --reset, or alone)")
    parser.add_argument("--snapshot", metavar="OUT.json", help="write a state snapshot of every app")
    parser.add_argument("--snapshot-only", action="store_true", help="with --snapshot: do not seed first")
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    load_dotenv(REPO_ROOT / ".env")
    env: Mapping[str, str] = dict(os.environ)
    scenario_id, seed_config = resolve_seed_config(task=args.task, scenario=args.scenario)
    apps, skipped = build_apps(str(args.apps).split(","), env)
    for entry in skipped:
        print(f"skipped:{entry}")
    if not apps:
        print("error: no apps to operate on", file=sys.stderr)
        return 2

    manifest_path = Path(args.manifest)
    exit_code = 0
    try:
        if args.reset:
            manifest = SeedManifest.load(manifest_path)
            for name, app in apps.items():
                await app.reset(manifest)
                print(f"reset:{name} scenario={manifest.scenario_id} seeded_at={manifest.seeded_at:.0f}")
        elif manifest_path.exists() and (args.verify or args.snapshot_only):
            manifest = SeedManifest.load(manifest_path)
            for app in apps.values():
                remember = cast(Callable[[SeedManifest], None] | None, getattr(app, "remember", None))
                if remember is not None:
                    remember(manifest)

        seeding = not args.reset and not args.verify and not args.snapshot_only
        if seeding:
            manifest = SeedManifest(scenario_id=scenario_id)
            expected = expected_counts(seed_config)
            for name, app in apps.items():
                result = await app.seed(seed_config, manifest)
                line, matches = format_counts(result, expected.get(name, {}))
                print(f"seeded:{name} {line}" + ("" if matches else "  MISMATCH"))
                for note in result.notes:
                    print(f"  note:{name} {note}")
                if not matches:
                    exit_code = 1
            manifest.dump(manifest_path)
            print(f"manifest:{manifest_path}")

        if args.verify:
            residue: list[str] = []
            for app in apps.values():
                residue.extend(await app.verify_clean())
            if residue:
                for item in residue:
                    print(f"residue:{item}")
                exit_code = 1
            else:
                print("clean")

        if args.snapshot:
            snapshot = {name: await app.snapshot() for name, app in apps.items()}
            out = Path(args.snapshot)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(snapshot, indent=2, sort_keys=True, default=str) + "\n")
            print(f"snapshot:{out}")
    finally:
        for app in apps.values():
            await app.aclose()
    return exit_code


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(run(args))
    except (ScratchGuardError, FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
