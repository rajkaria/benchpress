"""Real-app gateway for `github` (role `code_host`): host, headers, JSON bodies, redaction, guards.

No network: every request lands in `httpx.MockTransport`. Repositories and tokens are invented.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from benchpress.context import Context, TaskFrame
from benchpress.gate import Gate
from benchpress.playbooks.github import GitHubPlaybook
from benchpress.realapp import (
    PROVIDER_ROLES,
    REAL_BASE_URLS,
    TOKEN_ENV_VARS,
    RealAppConfig,
    RealAppGateway,
    ScratchGuardError,
    config_from_env,
    gateway_from_env,
)
from benchpress.tools import ToolBus

GITHUB_TOKEN = "ghp_unitTestToken0123456789abcdef"
SCRATCH_ENV = {"BENCHPRESS_SCRATCH_OK": "1"}
REPO = "/repos/example-org/widget-api"


class Recorder:
    def __init__(self, status: int = 200, payload: object | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.status = status
        self.payload: object = {"ok": True} if payload is None else payload

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json=self.payload)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def github_gateway(recorder: Recorder, **kwargs: Any) -> RealAppGateway:
    config = config_from_env("github", {"GITHUB_TOKEN": GITHUB_TOKEN})
    return RealAppGateway([config], env=SCRATCH_ENV, transport=recorder.transport, **kwargs)


def test_github_provider_constants() -> None:
    assert PROVIDER_ROLES["github"] == "code_host"
    assert REAL_BASE_URLS["github"] == "https://api.github.com"
    assert TOKEN_ENV_VARS["github"] == "GITHUB_TOKEN"


def test_config_from_env_uses_the_real_host_and_documented_headers() -> None:
    config = config_from_env("github", {"GITHUB_TOKEN": GITHUB_TOKEN})
    assert config.base_url == "https://api.github.com" and config.role == "code_host"
    assert config.default_body_encoding == "json" and not config.is_loopback
    assert config.headers == {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {GITHUB_TOKEN}",
    }
    assert GITHUB_TOKEN not in repr(config)


def test_missing_token_names_the_variable_and_twins_never_get_the_real_token() -> None:
    with pytest.raises(ValueError, match="GITHUB_TOKEN"):
        config_from_env("github", {})
    twin = config_from_env("github", {"DEVSIM_GITHUB_URL": "http://127.0.0.1:9", "GITHUB_TOKEN": GITHUB_TOKEN})
    assert twin.is_loopback and twin.base_url == "http://127.0.0.1:9"
    assert twin.headers["Accept"] == "application/vnd.github+json"
    assert GITHUB_TOKEN not in twin.headers["Authorization"]


def test_real_github_host_requires_scratch_opt_in() -> None:
    config = RealAppConfig.for_provider("github", base_url="https://api.github.com", token=GITHUB_TOKEN)
    with pytest.raises(ScratchGuardError):
        RealAppGateway([config], env={})
    assert RealAppGateway([config], env=SCRATCH_ENV).provider_names() == ("github",)


@pytest.mark.asyncio
async def test_get_request_shape_headers_and_role_alias() -> None:
    recorder = Recorder(payload=[{"number": 41, "title": "Upload stalls"}])
    gateway = github_gateway(recorder)
    result = await gateway.execute_tool(
        "provider_api",
        {"provider": "code_host", "method": "GET", "path": f"{REPO}/issues", "query": {"state": "open"}},
    )
    assert result["ok"] is True and result["provider"] == "github" and result["requested_provider"] == "code_host"
    assert result["body"] == [{"number": 41, "title": "Upload stalls"}]
    request = recorder.requests[0]
    assert request.url == httpx.URL(f"https://api.github.com{REPO}/issues?state=open")
    assert request.headers["accept"] == "application/vnd.github+json"
    assert request.headers["x-github-api-version"] == "2022-11-28"
    assert request.headers["authorization"] == f"Bearer {GITHUB_TOKEN}"
    assert request.headers["user-agent"].startswith("benchpress")  # GitHub rejects requests without a User-Agent
    await gateway.aclose()


@pytest.mark.asyncio
async def test_write_bodies_are_json_and_credentials_are_redacted() -> None:
    recorder = Recorder(payload={"id": 7001, "body": "ok", "echo": f"Bearer {GITHUB_TOKEN}"})
    gateway = github_gateway(recorder)
    result = await gateway.execute_tool(
        "provider_api",
        {
            "provider": "github",
            "method": "POST",
            "path": f"{REPO}/issues/41/comments",
            "body": {"body": "Could you attach the client version?"},
        },
    )
    request = recorder.requests[0]
    assert request.method == "POST" and request.headers["content-type"].startswith("application/json")
    assert json.loads(request.content) == {"body": "Could you attach the client version?"}
    assert result["body"]["echo"] == "[redacted]"
    for rendered in (json.dumps(result), json.dumps(gateway.trace), json.dumps(gateway.describe()), repr(gateway)):
        assert GITHUB_TOKEN not in rendered
    assert gateway.describe()["github"]["headers"]["Authorization"] == "Bearer <redacted>"
    await gateway.aclose()


@pytest.mark.asyncio
async def test_gateway_from_env_builds_github() -> None:
    recorder = Recorder()
    gateway = gateway_from_env(
        ["github"], env={"GITHUB_TOKEN": GITHUB_TOKEN, **SCRATCH_ENV}, transport=recorder.transport
    )
    assert gateway.roles() == {"code_host": "github"}
    await gateway.aclose()


@pytest.mark.asyncio
async def test_playbook_reads_through_the_real_app_gateway() -> None:
    """The playbook's request shapes survive the gateway's path validation and reach the documented routes."""

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"{REPO}/issues/41":
            return httpx.Response(
                200,
                json={
                    "number": 41,
                    "title": "Upload stalls",
                    "state": "open",
                    "labels": [{"name": "bug"}],
                    "repository_url": f"https://api.github.com{REPO}",
                },
            )
        if request.url.path == f"{REPO}/contents/CONTRIBUTING.md":
            return httpx.Response(200, json={"type": "file", "encoding": "base64", "content": "UmV2aWV3IGZpcnN0Lg=="})
        return httpx.Response(404, json={"message": "Not Found"})

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return respond(request)

    config = config_from_env("github", {"GITHUB_TOKEN": GITHUB_TOKEN})
    gateway = RealAppGateway([config], env=SCRATCH_ENV, transport=httpx.MockTransport(handler))
    context = Context(trial_id="t-realapp-github", providers=("github",))
    bus = ToolBus(context=context, execute=gateway.execute_tool, gate=Gate(context=context, allow_unplanned=True))
    bus.enter("P2")
    playbook = GitHubPlaybook()
    record = await playbook.read_record(bus, "issue:example-org/widget-api#41")
    assert record is not None and record.fields["labels"] == "bug" and record.fields["title"] == "Upload stalls"
    bus.enter("P1")
    sources = await playbook.policy_sources(bus, TaskFrame(observed_identifiers=("example-org/widget-api",)))
    assert [source.text for source in sources] == ["Review first."]
    assert all(request.method == "GET" and request.url.host == "api.github.com" for request in seen)
    await gateway.aclose()
