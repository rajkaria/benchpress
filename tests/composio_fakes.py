"""A Composio client double with the verified SDK 0.21.1 method signatures. No network, no account.

Tool metadata objects are the SDK's own `composio.client.types.Tool` models, built with `model_construct`.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Sequence
from typing import Any

from composio.client.types import Tool, ToolkitMinimal


def make_tool(slug: str, toolkit: str, *, tags: Sequence[str] = (), description: str = "") -> Tool:
    return Tool.model_construct(
        slug=slug,
        name=slug.replace("_", " ").title(),
        description=description or f"{slug} tool",
        tags=list(tags),
        toolkit=ToolkitMinimal.model_construct(slug=toolkit, name=toolkit.title(), logo=""),
        input_parameters={"type": "object", "properties": {"item_id": {"type": "string"}}},
        output_parameters={"type": "object"},
    )


def catalog() -> dict[str, Tool]:
    """A generic toolkit: untagged reads and writes, hint tags, and a toolkit-mismatched tool."""
    tools = [
        make_tool("TRACKER_LIST_ITEMS", "tracker"),
        make_tool("TRACKER_GET_ITEM", "tracker", tags=["readOnlyHint"], description="Read one item"),
        make_tool("TRACKER_CREATE_ITEM", "tracker"),
        make_tool("TRACKER_UPDATE_ITEM", "tracker", tags=["important"]),
        make_tool("TRACKER_DELETE_ITEM", "tracker"),
        make_tool("TRACKER_SYNC_ALL", "tracker", tags=["destructiveHint"]),
        make_tool("TRACKER_GET_AND_REMOVE_ITEM", "tracker", tags=["readOnlyHint"]),
        make_tool("TRACKER_FAIL_ITEM", "tracker"),
        make_tool("LEDGER_LIST_ENTRIES", "ledger"),
    ]
    return {tool.slug: tool for tool in tools}


class FakeProvider:
    """The provider contract the SDK uses: `set_execute_tool_fn` then `execute_tool(slug=..., ...)`."""

    def __init__(self) -> None:
        self.execute_tool: Callable[..., Any] | None = None

    def set_execute_tool_fn(self, execute_tool_fn: Callable[..., Any]) -> None:
        self.execute_tool = execute_tool_fn


class FakeTools:
    """`composio.core.models.tools.Tools` as far as Benchpress uses it."""

    def __init__(self, tools: dict[str, Tool] | None = None) -> None:
        self.catalog = tools if tools is not None else catalog()
        self.calls: list[tuple[str, dict[Any, Any], dict[str, Any]]] = []
        self.metadata_lookups: list[str] = []
        self.listings: list[dict[str, Any]] = []
        self.store: dict[str, object] = {}
        self.raise_on_execute: Exception | None = None
        self.provider = FakeProvider()
        self.provider.set_execute_tool_fn(self.execute)

    def execute(
        self,
        slug: str,
        arguments: dict[Any, Any],
        *,
        connected_account_id: str | None = None,
        custom_auth_params: object | None = None,
        custom_connection_data: object | None = None,
        user_id: str | None = None,
        text: str | None = None,
        version: str | None = None,
        dangerously_skip_version_check: bool | None = None,
        modifiers: object | None = None,
    ) -> dict[str, Any]:
        options = {
            "connected_account_id": connected_account_id,
            "user_id": user_id,
            "version": version,
            "dangerously_skip_version_check": dangerously_skip_version_check,
        }
        self.calls.append((slug, arguments, options))
        if self.raise_on_execute is not None:
            raise self.raise_on_execute
        if slug not in self.catalog:
            raise LookupError(f"tool {slug} not found")
        if "FAIL" in slug:
            return {"data": {}, "error": "upstream rejected the request", "successful": False}
        item_id = str(arguments.get("item_id", ""))
        if "DELETE" in slug or "REMOVE" in slug:
            self.store.pop(item_id, None)
        elif "CREATE" in slug or "UPDATE" in slug:
            self.store[item_id] = dict(arguments)
        return {"data": {"slug": slug, "item": self.store.get(item_id)}, "error": None, "successful": True}

    def get(self, user_id: str, *, tools: list[str] | None = None) -> list[Callable[..., Any]]:
        """Agentic-provider tools: each binds `partial(self.execute, ...)` now, as SDK `_wrap_execute_tool` does."""
        return [
            functools.partial(self.execute, slug, user_id=user_id, dangerously_skip_version_check=True)
            for slug in tools or []
        ]

    def get_raw_composio_tool_by_slug(self, slug: str) -> Tool:
        self.metadata_lookups.append(slug)
        if slug not in self.catalog:
            raise LookupError(f"tool {slug} not found")
        return self.catalog[slug]

    def get_raw_composio_tools(
        self,
        tools: list[str] | None = None,
        search: str | None = None,
        toolkits: list[str] | None = None,
        scopes: list[str] | None = None,
        limit: int | None = None,
    ) -> list[Tool]:
        self.listings.append({"tools": tools, "search": search, "toolkits": toolkits, "limit": limit})
        wanted = {toolkit.lower() for toolkit in toolkits or []}
        return [tool for tool in self.catalog.values() if not wanted or tool.toolkit.slug in wanted]


class FakeComposio:
    """`composio.Composio` as far as Benchpress uses it: a `tools` resource and other attributes."""

    def __init__(self, tools: FakeTools | None = None) -> None:
        self.tools = tools or FakeTools()
        self.provider = self.tools.provider
        self.toolkits = "toolkits-resource"
