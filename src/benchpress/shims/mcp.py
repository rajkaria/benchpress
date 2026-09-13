"""MCP servers as a Benchpress `ToolExecutor`.

The Benchpress loop speaks one harness-shaped tool, `provider_api`
(`{provider, method, path, query?, body?, body_encoding?, headers?}`), plus `provider_docs`.
This module maps that shape onto MCP `tools/list` and `tools/call`, one `ClientSession` per
provider name, so `run_trial` and the `ToolBus` can drive MCP tools with the gate unchanged.

Routes (per provider):

    GET              /tools                  tools/list, every tool with its class, method and route
    GET              /tools/{name}           one tool's description
    GET              /tools/{name}/call      tools/call on a *read* tool
    POST|PUT|PATCH   /tools/{name}/call      tools/call on a *write* tool
    DELETE           /tools/{name}/call      tools/call on a *destructive* tool

Arguments are the JSON body (a mapping); a GET without a body takes string arguments from `query`.

Classification. `readOnlyHint: true` is `read`; `destructiveHint: false` is `write`; everything else
is `destructive`, which is the MCP specification's own default for an unannotated tool
(`readOnlyHint` defaults to false, `destructiveHint` to true). The HTTP method must match the class
or the call is refused with 405 before it reaches the server. That is what keeps the loop's
invariant honest: the gate never inspects GETs, so a GET must never be able to reach a tool that
writes. Destructive tools map to DELETE, which the Benchpress gate refuses unconditionally, so a
destructive MCP tool cannot run through the loop at all. Annotations are hints written by the server;
pass `classes` to override them for servers you do not control.

Results come back in the gateway shape the tool bus interprets: `{ok, status_code, body, error}`.
`body` is `structuredContent` when present, else the parsed JSON of a single text block, else
`{"text": ..., "content": [...]}`. An `isError` result is status 422 with the tool's text as `error`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, cast
from urllib.parse import unquote

try:
    from mcp import Client, ClientSession
    from mcp.types import CallToolResult, PaginatedRequestParams, TextContent, Tool
except ImportError as exc:  # pragma: no cover - exercised only without the extra installed
    raise ImportError(
        "benchpress.shims.mcp needs the MCP SDK: install the extra with `pip install 'benchpress-agent[mcp]'`"
    ) from exc

from benchpress.shims.guard_policy import ToolClass
from benchpress.tools import PROVIDER_API, PROVIDER_DOCS, ToolExecutor

CLASS_METHODS: Mapping[ToolClass, frozenset[str]] = {
    "read": frozenset({"GET"}),
    "write": frozenset({"POST", "PUT", "PATCH"}),
    "destructive": frozenset({"DELETE"}),
}
_PRIMARY_METHOD: Mapping[ToolClass, str] = {"read": "GET", "write": "POST", "destructive": "DELETE"}
_MAX_LIST_PAGES = 50
_MAX_ERROR_CHARS = 500

SessionLike = ClientSession | Client


def classify_tool(tool: Tool, override: ToolClass | None = None) -> ToolClass:
    """The Benchpress class of an MCP tool: an explicit override, else its annotations, else the spec default."""
    if override is not None:
        return override
    annotations = tool.annotations
    if annotations is not None and annotations.read_only_hint is True:
        return "read"
    if annotations is not None and annotations.destructive_hint is False:
        return "write"
    return "destructive"


def tool_route(name: str) -> str:
    return f"/tools/{name}/call"


def describe_tool(provider: str, tool: Tool, tool_class: ToolClass) -> dict[str, Any]:
    annotations = tool.annotations.model_dump(mode="json", by_alias=True, exclude_none=True) if tool.annotations else {}
    return {
        "provider": provider,
        "name": tool.name,
        "title": tool.title or (tool.annotations.title if tool.annotations else None) or tool.name,
        "description": tool.description or "",
        "class": tool_class,
        "method": _PRIMARY_METHOD[tool_class],
        "path": tool_route(tool.name),
        "input_schema": tool.input_schema,
        "output_schema": tool.output_schema,
        "annotations": annotations,
    }


def result_body(result: CallToolResult) -> object:
    """The most useful JSON view of a tool result, without losing non-text blocks."""
    if result.structured_content is not None:
        return cast(object, result.structured_content)
    texts = [block.text for block in result.content if isinstance(block, TextContent)]
    if len(texts) == 1 and len(result.content) == 1:
        try:
            parsed = cast(object, json.loads(texts[0]))
        except ValueError:
            parsed = None
        if isinstance(parsed, (dict, list)):
            return cast(object, parsed)
    body: dict[str, object] = {"text": "\n".join(texts)}
    if len(texts) != len(result.content):
        body["content"] = [block.model_dump(mode="json", by_alias=True, exclude_none=True) for block in result.content]
    return body


def _arguments(tool_input: Mapping[str, Any]) -> dict[str, Any] | None:
    """Tool arguments: the JSON body, or (with no body) the query's string values. None if the body is not a mapping."""
    body = tool_input.get("body")
    source: Mapping[object, object]
    if body is None:
        query = tool_input.get("query")
        source = cast(Mapping[object, object], query) if isinstance(query, Mapping) else {}
    elif isinstance(body, Mapping):
        source = cast(Mapping[object, object], body)
    else:
        return None
    return {str(key): value for key, value in source.items()}


def _result_text(result: CallToolResult) -> str:
    return "\n".join(block.text for block in result.content if isinstance(block, TextContent))


@dataclass
class McpToolRouter:
    """Routes harness-shaped `provider_api` / `provider_docs` calls to MCP sessions keyed by provider."""

    sessions: Mapping[str, SessionLike]
    classes: Mapping[str, ToolClass] = field(default_factory=dict[str, ToolClass])
    _tools: dict[str, dict[str, Tool]] = field(default_factory=dict[str, dict[str, Tool]])

    # -- discovery ---------------------------------------------------------------------

    def session(self, provider: str) -> ClientSession | None:
        found = self.sessions.get(provider)
        if found is None:
            return None
        return found.session if isinstance(found, Client) else found

    async def tools(self, provider: str, *, refresh: bool = False) -> dict[str, Tool]:
        """Every tool a provider's server lists (all pages), cached until `refresh`."""
        if not refresh and provider in self._tools:
            return self._tools[provider]
        session = self.session(provider)
        if session is None:
            raise KeyError(provider)
        listed: dict[str, Tool] = {}
        cursor: str | None = None
        for _ in range(_MAX_LIST_PAGES):
            params = PaginatedRequestParams(cursor=cursor) if cursor else None
            page = await session.list_tools(params=params)
            for tool in page.tools:
                listed[tool.name] = tool
            cursor = page.next_cursor
            if not cursor:
                break
        self._tools[provider] = listed
        return listed

    def tool_class(self, provider: str, tool: Tool) -> ToolClass:
        override: ToolClass | None = self.classes.get(f"{provider}/{tool.name}")
        if override is None:
            override = self.classes.get(tool.name)
        return classify_tool(tool, override)

    async def _find_tool(self, provider: str, name: str) -> Tool | None:
        tool = (await self.tools(provider)).get(name)
        if tool is None:
            tool = (await self.tools(provider, refresh=True)).get(name)
        return tool

    # -- the executor ------------------------------------------------------------------

    async def execute(self, tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        if tool_name == PROVIDER_API:
            return await self._api(tool_input)
        if tool_name == PROVIDER_DOCS:
            return await self._docs(tool_input)
        return {"ok": False, "error": f"unknown tool {tool_name!r}; available tools: {PROVIDER_API}, {PROVIDER_DOCS}"}

    async def _api(self, tool_input: Mapping[str, Any]) -> dict[str, Any]:
        provider = str(tool_input.get("provider") or "")
        method = str(tool_input.get("method") or "GET").upper()
        raw_path = str(tool_input.get("path") or "")
        path = raw_path.split("?", 1)[0].rstrip("/") or "/"

        def respond(status: int, body: object, error: str | None = None, **extra: object) -> dict[str, Any]:
            return {
                "ok": 200 <= status < 300 and error is None,
                "provider": provider,
                "method": method,
                "path": raw_path,
                "status_code": status,
                "body": body,
                "error": error,
                **extra,
            }

        if self.session(provider) is None:
            known = ", ".join(sorted(self.sessions)) or "none"
            return respond(404, None, f"unknown provider {provider!r}; MCP providers: {known}")
        segments = [unquote(segment) for segment in path.strip("/").split("/")]
        try:
            if segments == ["tools"]:
                if method != "GET":
                    return respond(405, None, "tool discovery is GET /tools")
                tools = await self.tools(provider, refresh=True)
                listing = [describe_tool(provider, tool, self.tool_class(provider, tool)) for tool in tools.values()]
                return respond(200, {"tools": listing})
            if len(segments) == 2 and segments[0] == "tools":
                if method != "GET":
                    return respond(405, None, "tool description is GET /tools/{name}")
                tool = await self._find_tool(provider, segments[1])
                if tool is None:
                    return respond(404, None, f"unknown tool {segments[1]!r} on provider {provider!r}")
                return respond(200, describe_tool(provider, tool, self.tool_class(provider, tool)))
            if len(segments) == 3 and segments[0] == "tools" and segments[2] == "call":
                status, body, error, extra = await self._call(provider, method, segments[1], tool_input)
                return respond(status, body, error, **extra)
        except Exception as exc:  # noqa: BLE001 - transport failures are data, not crashes
            return respond(502, None, f"mcp:{type(exc).__name__}: {str(exc)[:_MAX_ERROR_CHARS]}")
        routes = "GET /tools, GET /tools/{name}, <method> /tools/{name}/call"
        return respond(404, None, f"no MCP route {path!r}; routes: {routes}")

    async def _call(
        self, provider: str, method: str, name: str, tool_input: Mapping[str, Any]
    ) -> tuple[int, object, str | None, dict[str, object]]:
        tool = await self._find_tool(provider, name)
        if tool is None:
            return 404, None, f"unknown tool {name!r} on provider {provider!r}", {}
        tool_class = self.tool_class(provider, tool)
        extra: dict[str, object] = {"tool": name, "tool_class": tool_class}
        if method not in CLASS_METHODS[tool_class]:
            reason = (
                f"method_mismatch: tool {name!r} is classified {tool_class}; call it with {_PRIMARY_METHOD[tool_class]}"
            )
            return 405, None, reason, extra
        arguments = _arguments(tool_input)
        if arguments is None:
            return 400, None, "the body must be a JSON object of tool arguments", extra
        session = self.session(provider)
        if session is None:  # pragma: no cover - checked by the caller
            return 404, None, f"unknown provider {provider!r}", extra
        result = await session.call_tool(name, arguments)
        if result.is_error:
            text = _result_text(result)[:_MAX_ERROR_CHARS] or "tool reported an error"
            return 422, result_body(result), f"tool_error: {text}", extra
        return 200, result_body(result), None, extra

    async def _docs(self, tool_input: Mapping[str, Any]) -> dict[str, Any]:
        """`search` lists matching tools across providers; `fetch` describes one (`provider` + `tool`)."""
        action = str(tool_input.get("action") or "search")
        try:
            if action == "fetch":
                provider = str(tool_input.get("provider") or "")
                name = str(tool_input.get("tool") or tool_input.get("name") or "")
                if self.session(provider) is None:
                    return {"ok": False, "error": f"unknown provider {provider!r}"}
                tool = await self._find_tool(provider, name)
                if tool is None:
                    return {"ok": False, "error": f"unknown tool {name!r} on provider {provider!r}"}
                return {"ok": True, "tool": describe_tool(provider, tool, self.tool_class(provider, tool))}
            needle = str(tool_input.get("query") or "").casefold()
            wanted = tool_input.get("provider")
            results: list[dict[str, Any]] = []
            for provider in sorted(self.sessions):
                if wanted and provider != wanted:
                    continue
                for tool in (await self.tools(provider)).values():
                    haystack = f"{tool.name}\n{tool.title or ''}\n{tool.description or ''}".casefold()
                    if needle and needle not in haystack:
                        continue
                    described = describe_tool(provider, tool, self.tool_class(provider, tool))
                    results.append(
                        {key: described[key] for key in ("provider", "name", "description", "class", "method", "path")}
                    )
            return {"ok": True, "results": results}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"mcp:{type(exc).__name__}: {str(exc)[:_MAX_ERROR_CHARS]}"}


def mcp_executor(
    sessions: Mapping[str, SessionLike],
    *,
    classes: Mapping[str, ToolClass] | None = None,
) -> ToolExecutor:
    """A Benchpress `ToolExecutor` over MCP sessions keyed by provider name.

    `sessions` values are initialized `mcp.ClientSession`s or entered `mcp.Client`s. `classes`
    overrides a tool's class by `"provider/tool"` or bare `"tool"` name (see the module docstring).
    """
    router = McpToolRouter(sessions=dict(sessions), classes=dict(classes or {}))
    return router.execute
