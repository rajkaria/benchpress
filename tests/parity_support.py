"""The parity fixture's scripted provider and its three drivers: the library, the HTTP gateway and the MCP gateway.

Each driver runs the committed calls (`tests/fixtures/parity`, built by `scripts/gen_gateway_parity.py`) in `n` order
against a fresh `ScriptedExecutor` under the fixture's fixed clock, and returns the `benchpress-write/1` lines it
produced, oldest first, with the status each call reported.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp import Client

from benchpress.context import Action
from benchpress.gateway.app import create_app
from benchpress.gateway.config import Settings
from benchpress.gateway.mcp_server import build_mcp_server
from benchpress.gateway.schemas import ContextInput
from benchpress.gateway.service import GatewayService
from benchpress.gateway.store import Store, WorkspaceRow
from benchpress.idempotency import InMemoryIdempotencyStore
from benchpress.phases.execute import fill_placeholders
from benchpress.tools import as_mapping
from benchpress.verified import VerifiedWrite
from benchpress.write_receipts import MemoryReceiptSink

__all__ = ["FIXTURE_DIR", "Fixture", "ScriptedExecutor", "http_lines", "library_lines", "load_fixture", "mcp_lines"]

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "parity"
Fixture = tuple[dict[str, Any], list[dict[str, Any]]]
NOT_FOUND: Mapping[str, Any] = {"status_code": 404, "body": {"message": "Not Found"}}


def load_fixture() -> Fixture:
    """The committed header (`sessions.json`) and calls (`calls.jsonl`, one per line, in `n` order)."""
    header = json.loads((FIXTURE_DIR / "sessions.json").read_text(encoding="utf-8"))
    lines = (FIXTURE_DIR / "calls.jsonl").read_text(encoding="utf-8").splitlines()
    return header, [json.loads(line) for line in lines if line.strip()]


def _without_query(path: str) -> str:
    return path.split("?", 1)[0]


class ScriptedExecutor:
    """A provider that answers only what the fixture scripts, keyed by `(method, path without query)`.

    A write returns its call's `write` script, or raises `ConnectionError` for `{"raise": message}`. A `GET` returns
    the `read` script of the call whose read-back path it is (placeholders filled from that call's write response),
    or 404 when that script is null. Any other request is unscripted: it raises `AssertionError`, which the tool bus
    reports as a failed call, and is kept in `unscripted` so `check()` fails the driver outright.
    """

    def __init__(self, calls: Sequence[Mapping[str, Any]]) -> None:
        self._responses: dict[tuple[str, str], Mapping[str, Any] | None] = {}
        self.unscripted: list[str] = []
        for call in calls:
            action = cast(Mapping[str, Any], call["action"])
            write = cast(Mapping[str, Any], call["write"])
            self._script(str(action["method"]), str(action["path"]), write)
            readback = cast("Mapping[str, Any] | None", action.get("readback"))
            if readback is not None:
                read_path = fill_placeholders(str(readback["path"]), as_mapping(write.get("body")))
                self._script("GET", read_path, cast("Mapping[str, Any] | None", call["read"]))

    def _script(self, method: str, path: str, response: Mapping[str, Any] | None) -> None:
        key = (method.upper(), _without_query(path))
        if key in self._responses and self._responses[key] != response:
            raise ValueError(f"two calls script {key[0]} {key[1]} differently")
        self._responses[key] = response

    async def execute_tool(self, tool_name: str, tool_input: dict[str, Any]) -> object:
        method = str(tool_input.get("method", "")).upper()
        path = _without_query(str(tool_input.get("path", "")))
        if (method, path) not in self._responses:
            self.unscripted.append(f"{method} {path}")
            raise AssertionError(f"unscripted {method} {path}")
        response = self._responses[(method, path)]
        if response is None:
            return copy.deepcopy(dict(NOT_FOUND))
        if "raise" in response:
            raise ConnectionError(str(response["raise"]))
        return {"status_code": response["status_code"], "body": copy.deepcopy(response["body"])}

    def check(self) -> None:
        if self.unscripted:
            raise AssertionError(f"unscripted provider requests: {', '.join(self.unscripted)}")


def _clock(header: Mapping[str, Any]) -> Callable[[], str]:
    at = str(header["clock"])
    return lambda: at


def _sessions(header: Mapping[str, Any]) -> dict[str, Any]:
    return cast(dict[str, Any], header["sessions"])


async def library_lines(fixture: Fixture) -> tuple[list[str], list[str]]:
    """One `VerifiedWrite` per session, sharing one idempotency store, every line into one memory sink."""
    header, calls = fixture
    executor = ScriptedExecutor(calls)
    claims = InMemoryIdempotencyStore()
    sink = MemoryReceiptSink()
    writers = {
        session_id: VerifiedWrite(
            executor.execute_tool,
            context=ContextInput.model_validate(context).to_context(session_id),
            allow_unplanned=True,
            idempotency=claims,
            scope=session_id,
            receipts=sink,
            clock=_clock(header),
            workspace=str(header["workspace"]),
            session=session_id,
        )
        for session_id, context in _sessions(header).items()
    }
    statuses: list[str] = []
    for call in calls:
        outcome = await writers[str(call["session"])].run(Action.model_validate(call["action"]))
        statuses.append(outcome.status)
    executor.check()
    return list(sink.lines), statuses


@dataclass
class _Gateway:
    app: FastAPI
    store: Store
    workspace: WorkspaceRow
    headers: dict[str, str]

    def open_sessions(self, client: TestClient, header: Mapping[str, Any]) -> None:
        for session_id, context in _sessions(header).items():
            created = client.post(
                "/v1/sessions", json={"session_id": session_id, "context": context}, headers=self.headers
            )
            if created.status_code != 201:
                raise AssertionError(f"POST /v1/sessions {session_id}: {created.status_code} {created.text}")

    def lines(self) -> list[str]:
        """The workspace's receipt lines, oldest first (the store lists newest first)."""
        return [row.line for row in reversed(self.store.receipts(self.workspace.id, limit=500))]


def _gateway(header: Mapping[str, Any], directory: Path, executor: ScriptedExecutor) -> _Gateway:
    directory.mkdir(parents=True, exist_ok=True)
    url = f"sqlite:///{directory / 'gateway.db'}"
    store = Store.open(url)
    workspace = store.create_workspace(str(header["workspace"]))
    _, key = store.create_api_key(workspace.id, "parity")
    app = create_app(Settings(store=url), store=store, executor=executor.execute_tool, clock=_clock(header))
    return _Gateway(app, store, workspace, {"Authorization": f"Bearer {key}"})


async def http_lines(fixture: Fixture, tmp_path: Path) -> tuple[list[str], list[str]]:
    """`POST /v1/sessions` with the fixture's session ids, then `POST /v1/execute` per call; lines from the store."""
    header, calls = fixture
    executor = ScriptedExecutor(calls)
    gateway = _gateway(header, tmp_path, executor)
    statuses: list[str] = []
    try:
        with TestClient(gateway.app) as client:
            gateway.open_sessions(client, header)
            for call in calls:
                body = {"session_id": call["session"], "action": call["action"]}
                response = client.post("/v1/execute", json=body, headers=gateway.headers)
                if response.status_code != 200:
                    raise AssertionError(f"call {call['n']}: POST /v1/execute {response.status_code} {response.text}")
                statuses.append(str(response.json()["status"]))
        executor.check()
        return gateway.lines(), statuses
    finally:
        gateway.store.engine.dispose()


async def mcp_lines(fixture: Fixture, tmp_path: Path) -> tuple[list[str], list[str]]:
    """Sessions over HTTP as above, then the in-process MCP server's `verified_write` per call; lines from the store."""
    header, calls = fixture
    executor = ScriptedExecutor(calls)
    gateway = _gateway(header, tmp_path, executor)

    async def authenticate(_header: str | None) -> WorkspaceRow:
        return gateway.workspace

    service = cast(GatewayService, gateway.app.state.service)
    statuses: list[str] = []
    try:
        with TestClient(gateway.app) as client:
            gateway.open_sessions(client, header)
            async with Client(build_mcp_server(service, authenticate)) as mcp:
                for call in calls:
                    arguments = {"action": call["action"], "session_id": call["session"]}
                    result = await mcp.call_tool("verified_write", arguments)
                    payload = result.structured_content
                    if result.is_error or payload is None:
                        raise AssertionError(f"call {call['n']}: verified_write failed: {result.content}")
                    statuses.append(str(payload["status"]))
        executor.check()
        return gateway.lines(), statuses
    finally:
        gateway.store.engine.dispose()
