"""`FakeArgaCli`: the harness's `ArgaCli` protocol, backed by local twins instead of Arga.

The unmodified `run_argabench_40.run_task` drives it exactly as it drives the real CLI:
`create_twin_run` → `control.json` → `wait_ready` (status) → gateway + state capture →
`wait_cleanup` (teardown, then status until terminal with `twins == {}`). Payload shape follows
`arga_cli/client.py:_parse_twin_run` (`run_id`, `status`, `is_public`, per twin `base_url`,
`admin_url`, `env_vars`) and `evaluation/state_capture.py:targets_from_control`
(`admin_url != base_url`, http(s) without path/query/credentials).

Scenario resolution reads the repo's `benchmark/argabench_40/scenarios/*.json`: every file carries
the `suite:argabench-40-v1` tag and a `content-sha256:` tag equal to `content_hash(task)`, so the
harness's own `resolve_scenarios` works against `list_scenarios` unchanged. The content digest is
also the twins' `seed_key`, so ids are deterministic per task.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from devsim.harness import CONTENT_HASH_TAG_PREFIX, harness_module, scenarios_dir
from devsim.server import RunningTwins, serve_twins
from devsim.twins import Store

READY = "ready"
TORN_DOWN = "torn_down"
SUBSTRATE = "devsim-local-twins"

# Env var names the gateway reads for each provider (providers/gateway.py:_PROVIDER_ENV_KEYS).
# `candidate_access` drops names containing "ADMIN", starting with "ARGA_", or "PROXY_TOKEN".
PROVIDER_TOKEN_ENV: dict[str, str] = {
    "discord": "DISCORD_TOKEN",
    "github": "GITHUB_TOKEN",
    "gitlab": "GITLAB_TOKEN",
    "gmail": "GMAIL_TOKEN",
    "google_calendar": "GOOGLE_CALENDAR_TOKEN",
    "google_drive": "GOOGLE_DRIVE_TOKEN",
    "hubspot": "HUBSPOT_ACCESS_TOKEN",
    "jira": "JIRA_TOKEN",
    "linear": "LINEAR_API_KEY",
    "linkedin": "LINKEDIN_ACCESS_TOKEN",
    "notion": "NOTION_TOKEN",
    "salesforce": "SALESFORCE_ACCESS_TOKEN",
    "slack": "SLACK_BOT_TOKEN",
    "stripe": "STRIPE_API_KEY",
}
_TOKEN_PREFIXES: dict[str, str] = {
    "github": "ghp_devsim",
    "gitlab": "glpat-devsim-",
    "gmail": "ya29.devsim-",
    "google_calendar": "ya29.devsim-calendar-",
    "google_drive": "ya29.devsim-drive-",
    "hubspot": "pat-na1-devsim-",
    "linear": "lin_api_devsim_",
    "notion": "secret_devsim_",
    "slack": "xoxb-devsim-",
    "stripe": "sk_test_devsim_",
}


class DevsimLifecycleError(LookupError):
    """Unknown run or scenario. Deliberately not an `ArgaCliError`: the harness must not retry it."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def token_env_name(provider: str) -> str:
    return PROVIDER_TOKEN_ENV.get(provider, f"{provider.upper()}_TOKEN")


def dummy_token(provider: str, run_id: str) -> str:
    digest = hashlib.sha256(f"{run_id}|{provider}".encode()).hexdigest()[:24]
    return f"{_TOKEN_PREFIXES.get(provider, f'devsim-{provider}-')}{digest}"


# --------------------------------------------------------------------------------------
# Scenario catalog
# --------------------------------------------------------------------------------------


def scenario_content_digest(scenario: Mapping[str, Any]) -> str:
    """The `content-sha256:` tag (falls back to a hash of the scenario itself)."""

    tags = scenario.get("tags")
    if isinstance(tags, list):
        for tag in cast(list[object], tags):
            if isinstance(tag, str) and tag.startswith(CONTENT_HASH_TAG_PREFIX):
                return tag.removeprefix(CONTENT_HASH_TAG_PREFIX)
    encoded = json.dumps(dict(scenario), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def scenario_id_for(scenario: Mapping[str, Any]) -> str:
    """Deterministic, readable stand-in for Arga's saved-Scenario id."""

    digest = scenario_content_digest(scenario)
    tags = scenario.get("tags")
    task_tag = None
    if isinstance(tags, list):
        task_tag = next(
            (
                tag.split(":", 1)[1]
                for tag in cast(list[object], tags)
                if isinstance(tag, str) and tag.startswith("task:")
            ),
            None,
        )
    return f"devsim-{task_tag}-{digest[:12]}" if task_tag else f"devsim-{digest[:16]}"


def load_scenario_catalog(directory: Path | None = None) -> list[dict[str, Any]]:
    """Read every `*.json` scenario, adding the fields Arga's list output carries."""

    folder = directory or scenarios_dir()
    catalog: list[dict[str, Any]] = []
    for path in sorted(folder.glob("*.json")):
        raw: object = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"scenario file {path} must contain a JSON object")
        scenario = dict(cast(dict[str, Any], raw))
        scenario.setdefault("id", scenario_id_for(scenario))
        scenario.setdefault("prompt", None)
        scenario.setdefault("is_preset", False)
        catalog.append(scenario)
    if not catalog:
        raise ValueError(f"no scenarios found under {folder}")
    return catalog


# --------------------------------------------------------------------------------------
# Runs
# --------------------------------------------------------------------------------------


@dataclass
class LocalTwinRun:
    run_id: str
    scenario_id: str
    seed_key: str
    providers: tuple[str, ...]
    ttl_minutes: int
    created_at: str
    status: str = READY
    running: RunningTwins | None = None
    torn_down_at: str | None = None
    history: list[str] = field(default_factory=lambda: list[str]())

    @property
    def stores(self) -> dict[str, Store]:
        return self.running.stores if self.running is not None else {}

    def store(self, provider: str) -> Store:
        return self.stores[provider]

    def twins_payload(self) -> dict[str, dict[str, Any]]:
        if self.running is None:
            return {}
        payload: dict[str, dict[str, Any]] = {}
        for provider, twin in sorted(self.running.twins.items()):
            payload[provider] = {
                "base_url": twin.base_url,
                "admin_url": twin.admin_url,
                "env_vars": {token_env_name(provider): dummy_token(provider, self.run_id)},
                "role": twin.role,
                "twin": "devsim-stub" if twin.is_stub else "devsim",
            }
        return payload

    def payload(self) -> dict[str, Any]:
        """The raw twin-run object `_parse_twin_run` reads and `control.json` stores verbatim."""

        seed_results = {
            provider: {"status": "seeded", "seed_key": self.seed_key} for provider in sorted(self.providers)
        }
        return {
            "run_id": self.run_id,
            "status": self.status,
            "is_public": True,
            "scenario_id": self.scenario_id,
            "twins": self.twins_payload(),
            "seed_results": seed_results if self.status == READY else {},
            "ttl_minutes": self.ttl_minutes,
            "created_at": self.created_at,
            "torn_down_at": self.torn_down_at,
            "substrate": SUBSTRATE,
        }


class FakeArgaCli:
    """Implements `arga_twins_benchmark.arga_cli.ArgaCli` over local twins."""

    def __init__(
        self,
        *,
        scenarios_dir: Path | None = None,
        catalog: Sequence[Mapping[str, Any]] | None = None,
    ) -> None:
        self.catalog: list[dict[str, Any]] = (
            [dict(item) for item in catalog] if catalog is not None else load_scenario_catalog(scenarios_dir)
        )
        self.runs: dict[str, LocalTwinRun] = {}
        self.calls: list[tuple[str, str]] = []
        self._counter = 0

    # -- protocol plumbing ---------------------------------------------------------------

    async def __aenter__(self) -> FakeArgaCli:
        return self

    async def __aexit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()

    def close(self) -> None:
        """No-op: `run_task` closes its CLI handle per trial while other trials keep running."""

    # -- scenarios -------------------------------------------------------------------------

    def scenario(self, scenario_id: str) -> dict[str, Any]:
        for item in self.catalog:
            if item.get("id") == scenario_id:
                return item
        raise DevsimLifecycleError(f"unknown scenario id {scenario_id!r}")

    def scenario_for_digest(self, digest: str) -> dict[str, Any]:
        for item in self.catalog:
            if scenario_content_digest(item) == digest:
                return item
        raise DevsimLifecycleError(f"no scenario carries content-sha256 {digest}")

    async def list_scenarios(self, *, tag: str) -> list[Mapping[str, Any]]:
        self.calls.append(("list_scenarios", tag))
        return [dict(item) for item in self.catalog if tag in cast(list[object], item.get("tags", []))]

    async def import_scenario(self, scenario_file: Path) -> Mapping[str, Any]:
        raw: object = json.loads(scenario_file.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("imported scenario must be a JSON object")
        scenario = dict(cast(dict[str, Any], raw))
        scenario["id"] = scenario_id_for(scenario)
        scenario.setdefault("prompt", None)
        scenario.setdefault("is_preset", False)
        self.catalog.append(scenario)
        self.calls.append(("import_scenario", scenario["id"]))
        return dict(scenario)

    # -- twin runs -------------------------------------------------------------------------

    def _twin_run(self, run: LocalTwinRun) -> Any:
        arga_cli = harness_module("arga_twins_benchmark.arga_cli")
        return arga_cli.TwinRun.from_payload(run.payload())

    def _run(self, run_id: str) -> LocalTwinRun:
        try:
            return self.runs[run_id]
        except KeyError as error:
            raise DevsimLifecycleError(f"unknown twin run {run_id!r}") from error

    async def create_twin_run(
        self,
        *,
        twins: Sequence[str],
        scenario_id: str,
        ttl_minutes: int = 60,
        candidate_safe: bool = False,
    ) -> Any:
        if not twins:
            raise ValueError("at least one twin is required")
        scenario = self.scenario(scenario_id)
        raw_seed = scenario.get("seed_config")
        seed_config: Mapping[str, Any] = cast(Mapping[str, Any], raw_seed) if isinstance(raw_seed, Mapping) else {}
        seed_key = scenario_content_digest(scenario)
        self._counter += 1
        run_id = f"devsim-run-{self._counter:04d}-{secrets.token_hex(4)}"
        providers = tuple(sorted(set(twins)))
        running = await serve_twins(providers, seed_config, seed_key)
        run = LocalTwinRun(
            run_id=run_id,
            scenario_id=scenario_id,
            seed_key=seed_key,
            providers=providers,
            ttl_minutes=ttl_minutes,
            created_at=utc_now(),
            running=running,
        )
        run.history.append("created")
        self.runs[run_id] = run
        self.calls.append(("create_twin_run", run_id))
        return self._twin_run(run)

    async def status(self, run_id: str) -> Any:
        self.calls.append(("status", run_id))
        return self._twin_run(self._run(run_id))

    async def reset(self, run_id: str) -> Mapping[str, Any]:
        """Re-seed by re-provisioning the twins (fresh stores, fresh ports) under the same run id."""

        run = self._run(run_id)
        if run.running is not None:
            await run.running.stop()
        scenario = self.scenario(run.scenario_id)
        raw_seed = scenario.get("seed_config")
        seed_config: Mapping[str, Any] = cast(Mapping[str, Any], raw_seed) if isinstance(raw_seed, Mapping) else {}
        run.running = await serve_twins(run.providers, seed_config, run.seed_key)
        run.status = READY
        run.torn_down_at = None
        run.history.append("reset")
        self.calls.append(("reset", run_id))
        return {**run.payload(), "reset": True}

    async def teardown(self, run_id: str) -> Mapping[str, Any]:
        run = self._run(run_id)
        if run.running is not None:
            await run.running.stop()
            run.running = None
        if run.status != TORN_DOWN:
            run.status = TORN_DOWN
            run.torn_down_at = utc_now()
            run.history.append("torn_down")
        self.calls.append(("teardown", run_id))
        return run.payload()

    async def stop_all(self) -> None:
        for run_id in list(self.runs):
            await self.teardown(run_id)


__all__ = [
    "PROVIDER_TOKEN_ENV",
    "READY",
    "SUBSTRATE",
    "TORN_DOWN",
    "DevsimLifecycleError",
    "FakeArgaCli",
    "LocalTwinRun",
    "dummy_token",
    "load_scenario_catalog",
    "scenario_content_digest",
    "scenario_id_for",
    "token_env_name",
]
