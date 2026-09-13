"""`benchpress.shims.composio.composio_executor`: Composio tools as a Benchpress ToolExecutor, against a fake client.

No network, no Composio account, no model.
"""

from __future__ import annotations

from typing import Any

from benchpress.context import Action, Context
from benchpress.gate import Gate
from benchpress.shims.composio import ComposioToolRouter, ToolClass, composio_executor, guard_composio
from benchpress.tools import ToolBus
from tests.composio_fakes import FakeComposio


def router(
    client: FakeComposio | None = None, *, classes: dict[str, ToolClass] | None = None, **kwargs: Any
) -> tuple[FakeComposio, ComposioToolRouter]:
    client = client or FakeComposio()
    return client, ComposioToolRouter(client=client, classes=classes or {}, **kwargs)


async def api(target: ComposioToolRouter, method: str, path: str, **extra: object) -> dict[str, Any]:
    tool_input = {"provider": "tracker", "method": method, "path": path, **extra}
    return await target.execute("provider_api", tool_input)


async def test_discovery_lists_the_toolkit_with_class_method_and_route() -> None:
    client, target = router(limit=250)
    listed = await api(target, "GET", "/tools")
    assert listed["ok"] and listed["status_code"] == 200
    tools = {tool["name"]: tool for tool in listed["body"]["tools"]}
    assert {name: (tool["class"], tool["method"]) for name, tool in tools.items()} == {
        "TRACKER_LIST_ITEMS": ("read", "GET"),
        "TRACKER_GET_ITEM": ("read", "GET"),
        "TRACKER_CREATE_ITEM": ("write", "POST"),
        "TRACKER_UPDATE_ITEM": ("write", "POST"),
        "TRACKER_DELETE_ITEM": ("destructive", "DELETE"),
        "TRACKER_SYNC_ALL": ("destructive", "DELETE"),
        "TRACKER_GET_AND_REMOVE_ITEM": ("destructive", "DELETE"),
        "TRACKER_FAIL_ITEM": ("write", "POST"),
    }
    assert client.tools.listings[-1] == {"tools": None, "search": None, "toolkits": ["tracker"], "limit": 250}
    get_item = tools["TRACKER_GET_ITEM"]
    assert get_item["path"] == "/tools/TRACKER_GET_ITEM/call" and get_item["tags"] == ["readOnlyHint"]
    assert get_item["toolkit"] == "tracker" and "item_id" in get_item["input_schema"]["properties"]
    one = await api(target, "GET", "/tools/TRACKER_GET_ITEM")
    assert one["body"]["class"] == "read" and one["body"]["description"] == "Read one item"


async def test_read_tool_via_get_with_body_or_query_and_execute_options() -> None:
    client, target = router(user_id="user-1", connected_account_id="ca-1", version="20260901_00")
    by_body = await api(target, "GET", "/tools/TRACKER_GET_ITEM/call", body={"item_id": "i-1"})
    assert by_body["ok"] and by_body["tool_class"] == "read" and by_body["body"]["slug"] == "TRACKER_GET_ITEM"
    by_query = await api(target, "GET", "/tools/TRACKER_LIST_ITEMS/call", query={"item_id": "i-2"})
    assert by_query["ok"]
    assert client.tools.calls[0] == (
        "TRACKER_GET_ITEM",
        {"item_id": "i-1"},
        {
            "connected_account_id": "ca-1",
            "user_id": "user-1",
            "version": "20260901_00",
            "dangerously_skip_version_check": None,
        },
    )
    assert client.tools.calls[1][1] == {"item_id": "i-2"}


async def test_a_get_can_never_reach_a_write_or_destructive_tool() -> None:
    client, target = router()
    for path in ("/tools/TRACKER_CREATE_ITEM/call", "/tools/TRACKER_SYNC_ALL/call"):
        refused = await api(target, "GET", path, body={"item_id": "i-1"})
        assert refused["status_code"] == 405 and not refused["ok"]
        assert str(refused["error"]).startswith("method_mismatch")
    assert (await api(target, "POST", "/tools/TRACKER_DELETE_ITEM/call", body={"item_id": "i-1"}))["status_code"] == 405
    assert (await api(target, "DELETE", "/tools/TRACKER_LIST_ITEMS/call"))["status_code"] == 405
    assert client.tools.calls == []


async def test_write_via_post_then_read_back() -> None:
    client, target = router()
    written = await api(target, "POST", "/tools/TRACKER_CREATE_ITEM/call", body={"item_id": "i-9", "title": "new"})
    assert written["ok"] and written["tool_class"] == "write"
    assert client.tools.store["i-9"] == {"item_id": "i-9", "title": "new"}
    read_back = await api(target, "GET", "/tools/TRACKER_GET_ITEM/call", body={"item_id": "i-9"})
    assert read_back["body"]["item"]["title"] == "new"


async def test_errors_unknowns_and_transport_failures() -> None:
    client, target = router(toolkits=("tracker",))
    failed = await api(target, "POST", "/tools/TRACKER_FAIL_ITEM/call", body={})
    assert failed["status_code"] == 422 and failed["error"] == "tool_error: upstream rejected the request"

    other = await target.execute("provider_api", {"provider": "ledger", "method": "GET", "path": "/tools"})
    assert other["status_code"] == 404 and "tracker" in str(other["error"])
    assert (await api(target, "GET", "/tools/TRACKER_NOPE/call"))["status_code"] == 404
    # a slug from another toolkit is not reachable through this provider
    assert (await api(target, "GET", "/tools/LEDGER_LIST_ENTRIES/call"))["status_code"] == 404
    assert (await api(target, "GET", "/v1/items"))["status_code"] == 404
    assert (await api(target, "POST", "/tools"))["status_code"] == 405
    assert (await api(target, "POST", "/tools/TRACKER_GET_ITEM"))["status_code"] == 405
    bad_body = await api(target, "POST", "/tools/TRACKER_CREATE_ITEM/call", body=["not", "a", "mapping"])
    assert bad_body["status_code"] == 400
    assert (await target.execute("shell", {}))["ok"] is False

    client.tools.raise_on_execute = TimeoutError("read timed out")
    broken = await api(target, "POST", "/tools/TRACKER_CREATE_ITEM/call", body={"item_id": "i-1"})
    assert broken["status_code"] == 502 and "TimeoutError" in str(broken["error"])


async def test_unlisted_tools_are_found_by_slug_and_classes_override() -> None:
    client, target = router(limit=1, classes={"TRACKER_SYNC_ALL": "write"})
    client.tools.get_raw_composio_tools = lambda **_: []  # type: ignore[method-assign]  # a truncated listing
    found = await api(target, "POST", "/tools/TRACKER_SYNC_ALL/call", body={})
    assert found["ok"] and found["tool_class"] == "write"
    assert client.tools.metadata_lookups == ["TRACKER_SYNC_ALL"]


async def test_docs_search_and_fetch() -> None:
    _, target = router(toolkits=("tracker",))
    found = await target.execute("provider_docs", {"action": "search", "query": "item"})
    names = {str(item["name"]) for item in found["results"]}
    assert found["ok"] and names >= {"TRACKER_GET_ITEM", "TRACKER_CREATE_ITEM", "TRACKER_DELETE_ITEM"}
    assert "LEDGER_LIST_ENTRIES" not in names
    fetched = await target.execute(
        "provider_docs", {"action": "fetch", "provider": "tracker", "tool": "TRACKER_DELETE_ITEM"}
    )
    assert fetched["tool"]["class"] == "destructive" and fetched["tool"]["method"] == "DELETE"
    missing = await target.execute("provider_docs", {"action": "fetch", "provider": "tracker", "tool": "NOPE"})
    assert missing["ok"] is False
    elsewhere = await target.execute("provider_docs", {"action": "fetch", "provider": "ledger", "tool": "X"})
    assert elsewhere["ok"] is False


async def test_a_guarded_client_refusal_surfaces_as_403() -> None:
    client = FakeComposio()
    guard_composio(client, {}, receipts=False)
    _, target = router(client)
    refused = await api(target, "POST", "/tools/TRACKER_CREATE_ITEM/call", body={"item_id": "i-1"})
    assert refused["status_code"] == 403 and str(refused["error"]).startswith("benchpress refused")
    assert client.tools.calls == []


async def test_the_tool_bus_and_gate_drive_composio_tools() -> None:
    """The loop's choke point: reads ungated, a write allowed once, a destructive tool refused in code."""
    client = FakeComposio()
    ctx = Context(providers=("tracker",))
    execute = composio_executor(client, user_id="user-1", toolkits=["Tracker"])
    bus = ToolBus(context=ctx, execute=execute, gate=Gate(ctx, allow_unplanned=True))
    bus.enter("P5")

    read = await bus.read("tracker", "/tools/TRACKER_GET_ITEM/call", body={"item_id": "i-1"})
    assert read.ok and read.json()["slug"] == "TRACKER_GET_ITEM"

    write = Action(
        id="a1",
        kind="update",
        provider="tracker",
        method="POST",
        path="/tools/TRACKER_UPDATE_ITEM/call",
        body={"item_id": "i-1", "status": "done"},
        fields=("item_id", "status"),
        satisfies=("d1",),
    )
    result, verdict = await bus.perform(write)
    assert verdict.allowed and result.ok and client.tools.store["i-1"] == {"item_id": "i-1", "status": "done"}
    replay, replay_verdict = await bus.perform(write)
    assert not replay_verdict.allowed and replay_verdict.rule == "idempotency" and not replay.ok

    destroy = Action(
        id="a2",
        kind="update",
        provider="tracker",
        method="DELETE",
        path="/tools/TRACKER_DELETE_ITEM/call",
        body={"item_id": "i-1"},
        satisfies=("d1",),
    )
    refused, refused_verdict = await bus.perform(destroy)
    assert not refused_verdict.allowed and refused_verdict.rule == "method" and not refused.ok

    # refused calls never left the process: two executions, both with the configured user
    assert [(slug, options["user_id"]) for slug, _, options in client.tools.calls] == [
        ("TRACKER_GET_ITEM", "user-1"),
        ("TRACKER_UPDATE_ITEM", "user-1"),
    ]
    assert "i-1" in client.tools.store
