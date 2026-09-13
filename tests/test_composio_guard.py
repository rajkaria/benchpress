"""`benchpress.shims.composio.guard_composio`: policy decisions before Composio executes, against a fake client.

No network, no Composio account. The fake mirrors the verified SDK 0.21.1 signatures, and the real SDK's
`Tools.execute` signature is checked here so the double cannot drift from it.
"""

from __future__ import annotations

import inspect
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from composio import Composio
from composio.core.models.tools import Tools

from benchpress.shims.composio import (
    COMPOSIO_SDK_VERSION,
    ComposioLike,
    GuardedComposio,
    classify_composio_tool,
    guard_composio,
)
from benchpress.shims.guard_policy import GuardPolicy, classify_tool_name
from benchpress.shims.openai_agents import classify_tool_name as agents_classify_tool_name
from tests.composio_fakes import FakeComposio, make_tool


def _sdk_client_satisfies_the_protocol(client: Composio[Any, Any]) -> ComposioLike:
    """Checked by pyright, not run: the real client type is assignable to the shim's protocol."""
    return client


def guarded(policy: dict[str, Any] | None = None, **kwargs: Any) -> tuple[FakeComposio, GuardedComposio]:
    client = FakeComposio()
    kwargs.setdefault("receipts", False)
    return client, guard_composio(client, policy or {}, **kwargs)


def test_the_fake_matches_the_verified_sdk_surface() -> None:
    assert callable(_sdk_client_satisfies_the_protocol)
    sdk_tools: Any = Tools
    assert tuple(int(part) for part in COMPOSIO_SDK_VERSION.split(".")[:2]) >= (0, 21)
    execute = inspect.signature(sdk_tools.execute).parameters
    assert list(execute)[:3] == ["self", "slug", "arguments"]
    for name in ("connected_account_id", "user_id", "text", "version", "dangerously_skip_version_check"):
        assert execute[name].kind is inspect.Parameter.KEYWORD_ONLY and execute[name].default is None
    assert list(inspect.signature(sdk_tools.get_raw_composio_tool_by_slug).parameters) == ["self", "slug"]
    listing = inspect.signature(sdk_tools.get_raw_composio_tools).parameters
    assert list(listing) == ["self", "tools", "search", "toolkits", "scopes", "limit"]


def test_the_name_heuristic_is_shared_with_the_openai_agents_shim() -> None:
    assert agents_classify_tool_name is classify_tool_name


def test_slug_classification_strips_the_toolkit_prefix_and_reads_hint_tags() -> None:
    tool = make_tool
    assert classify_composio_tool("TRACKER_LIST_ITEMS", tool("TRACKER_LIST_ITEMS", "tracker")) == "read"
    assert classify_composio_tool("TRACKER_CREATE_ITEM", tool("TRACKER_CREATE_ITEM", "tracker")) == "write"
    assert classify_composio_tool("TRACKER_DELETE_ITEM", tool("TRACKER_DELETE_ITEM", "tracker")) == "destructive"
    assert classify_composio_tool("TRACKER_POST_NOTE", tool("TRACKER_POST_NOTE", "tracker", tags=["readOnlyHint"])) == (
        "read"
    )
    synced = tool("TRACKER_SYNC_ALL", "tracker", tags=["destructiveHint"])
    assert classify_composio_tool("TRACKER_SYNC_ALL", synced) == "destructive"
    # a destructive verb beats a read-only tag
    assert classify_composio_tool("TRACKER_GET_AND_REMOVE_ITEM", tool("X", "tracker", tags=["readOnlyHint"])) == (
        "destructive"
    )
    # multi-token toolkit known from metadata
    assert classify_composio_tool("TEAM_WIKI_GET_PAGE", tool("TEAM_WIKI_GET_PAGE", "team_wiki")) == "read"
    # without metadata only the first token is dropped, which errs toward write, never toward read
    assert classify_composio_tool("TRACKER_FETCH_ITEM") == "read"
    assert classify_composio_tool("TEAM_WIKI_GET_PAGE") == "write"
    assert classify_composio_tool("LIST") == "read"


def test_declared_classes_beat_policy_classes_beat_metadata() -> None:
    policy = {"classes": {"TRACKER_UPDATE_ITEM": "read", "TRACKER_LIST_ITEMS": "destructive"}}
    client, composio = guarded(policy, classes={"TRACKER_UPDATE_ITEM": "destructive"})
    assert composio.guard.classify("TRACKER_UPDATE_ITEM") == "destructive"
    assert composio.guard.classify("TRACKER_LIST_ITEMS") == "destructive"
    assert composio.guard.classify("TRACKER_GET_ITEM") == "read"
    assert client.tools.metadata_lookups == ["TRACKER_GET_ITEM"]
    _, no_metadata = guarded(metadata=False)
    assert no_metadata.guard.classify("TRACKER_SYNC_ALL") == "write"


def test_reads_run_and_writes_are_refused_in_the_sdk_response_shape(tmp_path: Path) -> None:
    receipts = tmp_path / "receipts.jsonl"
    client, composio = guarded(receipts=receipts)

    read = composio.tools.execute("TRACKER_GET_ITEM", {"item_id": "i-1"}, user_id="user-1")
    assert read["successful"] is True

    refused = composio.tools.execute("TRACKER_CREATE_ITEM", {"item_id": "i-2", "title": "secret title"}, user_id="u")
    assert refused == {
        "data": {},
        "error": "benchpress refused 'TRACKER_CREATE_ITEM' [no_allow_rule]: 'TRACKER_CREATE_ITEM' is classified "
        "write and no policy rule allows it",
        "successful": False,
    }
    assert [call[0] for call in client.tools.calls] == ["TRACKER_GET_ITEM"]
    assert client.tools.calls[0][2]["user_id"] == "user-1"

    lines = [json.loads(line) for line in receipts.read_text(encoding="utf-8").splitlines()]
    assert [(line["tool"], line["class"], line["decision"], line["rule"]) for line in lines] == [
        ("TRACKER_GET_ITEM", "read", "allow", "read", ),
        ("TRACKER_CREATE_ITEM", "write", "refuse", "no_allow_rule"),
    ]  # fmt: skip
    assert lines[0]["upstream_error"] is False and lines[1]["upstream_error"] is None
    assert "secret title" not in receipts.read_text(encoding="utf-8")
    assert lines[1]["args_digest"].startswith("sha256:")
    assert composio.receipts == lines


def test_allow_rules_argument_regexes_and_max_calls() -> None:
    policy = {"rules": [{"tool": "TRACKER_UPDATE_*", "arguments": {"item_id": r"i-\d+"}, "max_calls": 2}]}
    client, composio = guarded(policy)
    assert composio.tools.execute("TRACKER_UPDATE_ITEM", {"item_id": "i-1"})["successful"]
    mismatch = composio.tools.execute("TRACKER_UPDATE_ITEM", {"item_id": "other"})
    assert "[arguments_mismatch]" in mismatch["error"]
    assert composio.tools.execute("TRACKER_UPDATE_ITEM", {"item_id": "i-2"})["successful"]
    capped = composio.tools.execute("TRACKER_UPDATE_ITEM", {"item_id": "i-3"})
    assert "[max_calls]" in capped["error"]
    assert [call[1]["item_id"] for call in client.tools.calls] == ["i-1", "i-2"]
    # rule globs are case-sensitive over upper-case slugs
    assert (
        "[no_allow_rule]"
        in guarded({"rules": [{"tool": "tracker_*"}]})[1].tools.execute("TRACKER_CREATE_ITEM", {})["error"]
    )


def test_destructive_tools_need_an_explicit_allow_destructive() -> None:
    client, composio = guarded({"rules": [{"tool": "*"}]})
    assert composio.tools.execute("TRACKER_CREATE_ITEM", {"item_id": "i-1"})["successful"]
    for slug in ("TRACKER_DELETE_ITEM", "TRACKER_SYNC_ALL", "TRACKER_GET_AND_REMOVE_ITEM"):
        assert "[destructive_default_deny]" in composio.tools.execute(slug, {"item_id": "i-1"})["error"]
    assert [call[0] for call in client.tools.calls] == ["TRACKER_CREATE_ITEM"]

    deny_first = {"rules": [{"tool": "TRACKER_SYNC_*", "effect": "deny"}, {"tool": "*", "allow_destructive": True}]}
    client, composio = guarded(deny_first)
    assert "[deny_rule]" in composio.tools.execute("TRACKER_SYNC_ALL", {})["error"]
    assert composio.tools.execute("TRACKER_DELETE_ITEM", {"item_id": "i-1"})["successful"]


def test_in_place_guarding_closes_the_client_and_provider_entry_points() -> None:
    client, composio = guarded()
    # the original client object is guarded too
    assert "[no_allow_rule]" in client.tools.execute("TRACKER_CREATE_ITEM", {"item_id": "i-1"})["error"]
    # the provider path (handle_tool_calls / agentic tools) keeps the SDK's skip-version contract
    execute_tool = client.provider.execute_tool
    assert execute_tool is not None
    assert "[no_allow_rule]" in execute_tool(slug="TRACKER_DELETE_ITEM", arguments={}, user_id="u")["error"]
    read = execute_tool(slug="TRACKER_LIST_ITEMS", arguments={}, user_id="u")
    assert read["successful"] and client.tools.calls[-1][2]["dangerously_skip_version_check"] is True
    assert len(composio.receipts) == 3

    plain_client = FakeComposio()
    guard_composio(plain_client, {}, receipts=False, in_place=False)
    assert plain_client.tools.execute("TRACKER_CREATE_ITEM", {"item_id": "i-1"})["successful"]


def test_invalid_arguments_and_upstream_failures_are_recorded() -> None:
    client, composio = guarded({"rules": [{"tool": "TRACKER_*"}]})
    bad: Any = ["not", "a", "mapping"]
    assert "[invalid_arguments]" in composio.tools.execute("TRACKER_CREATE_ITEM", bad)["error"]

    failed = composio.tools.execute("TRACKER_FAIL_ITEM", {})
    assert failed["successful"] is False and failed["error"] == "upstream rejected the request"

    client.tools.raise_on_execute = RuntimeError("connection reset")
    with pytest.raises(RuntimeError):
        composio.tools.execute("TRACKER_CREATE_ITEM", {"item_id": "i-1"})
    assert [(line["rule"], line["upstream_error"]) for line in composio.receipts] == [
        ("invalid_arguments", None),
        ("allow_rule", True),
        ("allow_rule", True),
    ]


def test_everything_else_is_forwarded_to_the_client() -> None:
    client, composio = guarded()
    assert composio.toolkits == "toolkits-resource"
    assert composio.tools.get_raw_composio_tools(toolkits=["ledger"])[0].slug == "LEDGER_LIST_ENTRIES"
    assert composio.tools.provider is client.provider


def test_policy_path_resolves_receipts_next_to_the_policy(tmp_path: Path) -> None:
    policy_file = tmp_path / "guard.json"
    policy_file.write_text(json.dumps({"receipts": "logs/composio.jsonl", "reads": "deny"}), encoding="utf-8")
    client = FakeComposio()
    composio = guard_composio(client, policy_file)
    assert "[reads_denied]" in composio.tools.execute("TRACKER_LIST_ITEMS", {})["error"]
    assert (tmp_path / "logs" / "composio.jsonl").is_file()

    composio = guard_composio(FakeComposio(), GuardPolicy(), receipts=False)
    assert composio.tools.execute("TRACKER_LIST_ITEMS", {})["successful"]


def test_import_benchpress_does_not_import_composio() -> None:
    code = "import sys, benchpress, benchpress.tools, benchpress.shims.guard_policy\nprint('composio' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


def test_agentic_tools_bind_execute_when_fetched_so_guard_first() -> None:
    client = FakeComposio()
    (fetched_before,) = client.tools.get("user-1", tools=["TRACKER_CREATE_ITEM"])
    guard_composio(client, {}, receipts=False)
    (fetched_after,) = client.tools.get("user-1", tools=["TRACKER_CREATE_ITEM"])
    assert "[no_allow_rule]" in fetched_after({"item_id": "i-1"})["error"]
    assert client.tools.calls == []
    # documented limit: a tool object fetched before guarding keeps the unguarded bound method
    assert fetched_before({"item_id": "i-1"})["successful"] is True
