"""Benchpress authority in front of Composio tools.

    from composio import Composio
    from benchpress.shims.composio import guard_composio

    composio = guard_composio(Composio(), "guard.json")
    composio.tools.execute("GITHUB_CREATE_AN_ISSUE", {"owner": "...", "repo": "...", "title": "..."}, user_id="u1")

`guard_composio` decides every tool execution in code before Composio sees it. By default it guards the
client **in place**: `client.tools.execute` and the provider's injected execute function (the one
`provider.handle_tool_calls` and agentic-provider tools call) are replaced with the guarded entry point,
so the original client object cannot bypass the policy. The returned `GuardedComposio` exposes the same
`tools.execute(slug, arguments, *, user_id=..., ...)` and forwards every other attribute to the client.

* **Class.** `classes` passed here (by slug), else the policy's `classes`, else Composio tool metadata
  (`tags` carrying an MCP-style `readOnlyHint` / `destructiveHint`), else the shared name heuristic
  `classify_tool_name` applied to the slug with its toolkit prefix removed (`GITHUB_LIST_ISSUES` is
  judged as `LIST_ISSUES`). A destructive verb in the name always wins over a read-only tag.
* **Decision.** The shared `GuardPolicy` (`benchpress.shims.guard_policy`): deny rules, allow rules with
  full-match argument regexes and `max_calls`, destructive refused unless a rule sets `allow_destructive`,
  reads per `policy.reads`. Rule globs match the slug case-sensitively (Composio slugs are upper case).
* **Refusal.** The SDK's own response shape, never an exception:
  `{"data": {}, "error": "benchpress refused 'GITHUB_DELETE_A_REPOSITORY' [...]: ...", "successful": False}`.
* **Receipts.** One JSONL line per call (tool slug, class, argument digest, decision, rule, reason,
  latency, `upstream_error`). Argument values are never stored.

`composio_executor(client, user_id=...)` is the second half: a Benchpress `ToolExecutor`, so
`benchpress.wrap(...)` drives Composio tools through the mutation gate. Providers are toolkit slugs:

    GET              /tools                  every tool of the toolkit with its class, method and route
    GET              /tools/{slug}           one tool's description
    GET              /tools/{slug}/call      tools.execute on a *read* tool
    POST|PUT|PATCH   /tools/{slug}/call      tools.execute on a *write* tool
    DELETE           /tools/{slug}/call      tools.execute on a *destructive* tool

The method must match the class (405 otherwise) so a GET, which the gate never inspects, can never reach
a tool that writes, and a destructive tool maps to DELETE, which the gate refuses unconditionally.

Verified against the `composio` Python SDK 0.21.1 (`Composio().tools`: `execute`,
`get_raw_composio_tool_by_slug`, `get_raw_composio_tools`, `provider.set_execute_tool_fn`).
"""

from __future__ import annotations

import asyncio
import functools
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, TypeAlias, cast, runtime_checkable
from urllib.parse import unquote

try:
    from composio import __version__ as COMPOSIO_SDK_VERSION
except ImportError as exc:  # pragma: no cover - exercised only without the extra installed
    raise ImportError(
        "benchpress.shims.composio needs the Composio SDK: install the extra with "
        "`pip install 'benchpress-agent[composio]'`"
    ) from exc

from benchpress.shims.guard_policy import (
    Decision,
    GuardPolicy,
    PolicyGuard,
    ToolClass,
    classify_tool_name,
    refusal_message,
)
from benchpress.tools import PROVIDER_API, PROVIDER_DOCS, ToolExecutor

__all__ = [
    "CLASS_METHODS",
    "COMPOSIO_SDK_VERSION",
    "DEFAULT_RECEIPTS_NAME",
    "ComposioClassifier",
    "ComposioGuard",
    "ComposioLike",
    "ComposioToolRouter",
    "ComposioToolsLike",
    "GuardedComposio",
    "GuardedTools",
    "classify_composio_tool",
    "composio_executor",
    "guard_composio",
    "refusal_response",
]

DEFAULT_RECEIPTS_NAME = "composio-guard-receipts.jsonl"

CLASS_METHODS: Mapping[ToolClass, frozenset[str]] = {
    "read": frozenset({"GET"}),
    "write": frozenset({"POST", "PUT", "PATCH"}),
    "destructive": frozenset({"DELETE"}),
}
_PRIMARY_METHOD: Mapping[ToolClass, str] = {"read": "GET", "write": "POST", "destructive": "DELETE"}
_READ_ONLY_TAGS = frozenset({"readonlyhint", "readonly"})
_DESTRUCTIVE_TAGS = frozenset({"destructivehint", "destructive"})
_MAX_ERROR_CHARS = 500
_REFUSAL_PREFIX = "benchpress refused "


# -- the verified SDK surface -----------------------------------------------------------------


@runtime_checkable
class ComposioToolsLike(Protocol):
    """The part of `composio.core.models.tools.Tools` (SDK 0.21.1) the shim uses."""

    def execute(
        self,
        slug: str,
        arguments: dict[Any, Any],
        *,
        connected_account_id: str | None = None,
        user_id: str | None = None,
        text: str | None = None,
        version: str | None = None,
        dangerously_skip_version_check: bool | None = None,
    ) -> Any: ...

    def get_raw_composio_tool_by_slug(self, slug: str) -> Any: ...

    def get_raw_composio_tools(
        self,
        tools: list[str] | None = None,
        search: str | None = None,
        toolkits: list[str] | None = None,
        scopes: list[str] | None = None,
        limit: int | None = None,
    ) -> list[Any]: ...


class ComposioLike(Protocol):
    """A `composio.Composio` client: anything with a `tools` resource of the verified shape."""

    @property
    def tools(self) -> ComposioToolsLike: ...


def _field(obj: object, name: str) -> object:
    if isinstance(obj, Mapping):
        return cast(Mapping[str, object], obj).get(name)
    return getattr(obj, name, None)


def _text(obj: object, name: str) -> str | None:
    value = _field(obj, name)
    return value if isinstance(value, str) and value else None


def _tags(tool: object) -> list[str]:
    raw = _field(tool, "tags")
    if not isinstance(raw, list | tuple):
        return []
    return [str(tag) for tag in cast(Sequence[object], raw)]


def toolkit_of(tool: object) -> str | None:
    """The tool's toolkit slug from its metadata (`tool.toolkit.slug`), lower case."""
    slug = _text(_field(tool, "toolkit"), "slug")
    return slug.lower() if slug else None


def _normalized_tag(tag: str) -> str:
    return "".join(char for char in tag.lower() if char.isalnum())


def _action_name(slug: str, toolkit: str | None) -> str:
    """The slug without its toolkit prefix: known from metadata, else the first `_` token."""
    if toolkit and slug.lower().startswith(toolkit.lower() + "_"):
        return slug[len(toolkit) + 1 :]
    head, sep, rest = slug.partition("_")
    return rest if sep and rest and head else slug


def classify_composio_tool(slug: str, tool: object | None = None) -> ToolClass:
    """A Composio tool's class from its metadata tags, else from its slug (toolkit prefix removed).

    A destructive tag or a destructive verb anywhere in the slug is destructive; a read-only tag, or a
    leading read verb after the toolkit prefix, is read; anything else is write.
    """
    by_name = classify_tool_name(_action_name(slug, toolkit_of(tool) if tool is not None else None))
    tags = {_normalized_tag(tag) for tag in _tags(tool)} if tool is not None else set[str]()
    if tags & _DESTRUCTIVE_TAGS or by_name == "destructive":
        return "destructive"
    if tags & _READ_ONLY_TAGS:
        return "read"
    return by_name


@dataclass
class ComposioClassifier:
    """Declared classes, then (lazily fetched, cached) tool metadata, then the slug heuristic."""

    tools: ComposioToolsLike
    classes: Mapping[str, ToolClass] = field(default_factory=dict[str, ToolClass])
    metadata: bool = True
    _cache: dict[str, object | None] = field(default_factory=dict[str, object | None])

    def remember(self, slug: str, tool: object) -> None:
        self._cache[slug] = tool

    def tool(self, slug: str) -> object | None:
        """Tool metadata by slug (the same lookup the SDK's own `execute` makes), None when unavailable."""
        if not self.metadata:
            return None
        if slug not in self._cache:
            try:
                self._cache[slug] = self.tools.get_raw_composio_tool_by_slug(slug)
            except Exception:  # noqa: BLE001 - no metadata means the heuristic decides
                self._cache[slug] = None
        return self._cache[slug]

    def classify(self, slug: str, extra: Mapping[str, ToolClass] | None = None) -> ToolClass:
        declared = self.classes.get(slug) or (extra or {}).get(slug)
        if declared is not None:
            return declared
        return classify_composio_tool(slug, self.tool(slug))


# -- stage 1: the guard -----------------------------------------------------------------------


def refusal_response(slug: str, decision: Decision) -> dict[str, Any]:
    """A refusal in the SDK's `ToolExecutionResponse` shape (`data`, `error`, `successful`)."""
    return {"data": {}, "error": refusal_message(slug, decision), "successful": False}


ExecuteFn: TypeAlias = Callable[..., Any]


@dataclass
class ComposioGuard:
    """One policy, its counters, its classifier and its receipts."""

    guard: PolicyGuard
    classifier: ComposioClassifier
    receipts: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    def classify(self, slug: str) -> ToolClass:
        return self.classifier.classify(slug, self.guard.policy.classes)

    def decide(self, slug: str, arguments: object) -> tuple[Decision, dict[str, Any]]:
        tool_class = self.classify(slug)
        if not isinstance(arguments, Mapping):
            return Decision(False, tool_class, "invalid_arguments", "arguments must be a mapping"), {}
        parsed = {str(key): value for key, value in cast(Mapping[object, object], arguments).items()}
        return self.guard.decide_class(slug, tool_class, parsed), parsed

    def execute(self, inner: ExecuteFn, slug: str, arguments: object, /, **options: Any) -> Any:
        """Decide, then (only when allowed) call `inner(slug, arguments, **options)`; record either way."""
        started = time.monotonic()
        decision, parsed = self.decide(slug, arguments)
        if not decision.allowed:
            self.receipts.append(self.guard.record(slug, parsed, decision, started, upstream_error=None))
            return refusal_response(slug, decision)
        try:
            response = inner(slug, arguments, **options)
        except Exception:
            self.receipts.append(self.guard.record(slug, parsed, decision, started, upstream_error=True))
            raise
        failed = _field(response, "successful") is False
        self.receipts.append(self.guard.record(slug, parsed, decision, started, upstream_error=failed))
        return response


class GuardedTools:
    """`client.tools` with `execute` guarded. Every other attribute is the client's own."""

    def __init__(self, tools: ComposioToolsLike, guard: ComposioGuard, inner: ExecuteFn) -> None:
        self._tools = tools
        self._inner = inner
        self.guard = guard

    def execute(self, slug: str, arguments: dict[Any, Any], **options: Any) -> Any:
        return self.guard.execute(self._inner, slug, arguments, **options)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._tools, name)


class GuardedComposio:
    """The client with `tools` guarded. Every other attribute is the client's own."""

    def __init__(self, client: ComposioLike, tools: GuardedTools) -> None:
        self._client = client
        self.tools = tools

    @property
    def guard(self) -> ComposioGuard:
        return self.tools.guard

    @property
    def receipts(self) -> list[dict[str, Any]]:
        return self.tools.guard.receipts

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


def _policy_and_base(policy: GuardPolicy | Mapping[str, Any] | str | Path) -> tuple[GuardPolicy, Path]:
    if isinstance(policy, GuardPolicy):
        return policy, Path.cwd()
    if isinstance(policy, str | Path):
        return GuardPolicy.load(policy), Path(policy).resolve().parent
    return GuardPolicy.model_validate(dict(policy)), Path.cwd()


def guard_composio(
    client: ComposioLike,
    policy: GuardPolicy | Mapping[str, Any] | str | Path,
    *,
    receipts: str | Path | Literal[False] | None = None,
    classes: Mapping[str, ToolClass] | None = None,
    metadata: bool = True,
    in_place: bool = True,
) -> GuardedComposio:
    """Put a guard policy in front of every Composio tool execution.

    `policy` is a `GuardPolicy`, its dict form, or a path to the JSON file. `receipts` is the JSONL path;
    by default the policy's `receipts` (relative to the policy file when loaded from a path, else the
    working directory), else `composio-guard-receipts.jsonl` there; `False` keeps lines in memory only
    (`guarded.receipts`). `classes` maps slugs to classes ahead of the policy's classes, metadata tags and
    the slug heuristic. `metadata=False` never looks tool metadata up. `in_place=True` (default) also
    rebinds `client.tools.execute` and the provider's execute function to the guarded entry point.
    """
    loaded, base = _policy_and_base(policy)
    if receipts is False:
        receipts_path = None
    elif receipts is not None:
        receipts_path = Path(receipts)
    else:
        receipts_path = base / (loaded.receipts or DEFAULT_RECEIPTS_NAME)
    sdk_tools = client.tools
    guard = ComposioGuard(
        guard=PolicyGuard(loaded, receipts_path=receipts_path),
        classifier=ComposioClassifier(tools=sdk_tools, classes=dict(classes or {}), metadata=metadata),
    )
    inner: ExecuteFn = sdk_tools.execute
    guarded = GuardedTools(sdk_tools, guard, inner)
    if in_place:
        setattr(sdk_tools, "execute", guarded.execute)  # noqa: B010 - rebinding the instance's entry point
        provider = getattr(sdk_tools, "provider", None)
        set_execute = getattr(provider, "set_execute_tool_fn", None)
        if callable(set_execute):
            # the SDK injects `partial(tools.execute, dangerously_skip_version_check=True)`; keep that contract
            set_execute(functools.partial(guarded.execute, dangerously_skip_version_check=True))
    return GuardedComposio(client, guarded)


# -- stage 2: the executor --------------------------------------------------------------------


def tool_route(slug: str) -> str:
    return f"/tools/{slug}/call"


def describe_tool(provider: str, slug: str, tool: object, tool_class: ToolClass) -> dict[str, Any]:
    return {
        "provider": provider,
        "name": slug,
        "title": _text(tool, "name") or slug,
        "description": _text(tool, "description") or "",
        "class": tool_class,
        "method": _PRIMARY_METHOD[tool_class],
        "path": tool_route(slug),
        "toolkit": toolkit_of(tool) or provider,
        "tags": _tags(tool),
        "input_schema": _field(tool, "input_parameters"),
        "output_schema": _field(tool, "output_parameters"),
    }


def _arguments(tool_input: Mapping[str, Any]) -> dict[str, Any] | None:
    """Tool arguments: the JSON body, or (with no body) the query's values. None if the body is not a mapping."""
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


@dataclass
class ComposioToolRouter:
    """Routes harness-shaped `provider_api` / `provider_docs` calls to Composio tools, one provider per toolkit."""

    client: ComposioLike
    user_id: str | None = None
    connected_account_id: str | None = None
    version: str | None = None
    dangerously_skip_version_check: bool | None = None
    toolkits: tuple[str, ...] = ()
    classes: Mapping[str, ToolClass] = field(default_factory=dict[str, ToolClass])
    limit: int = 1000
    _tools: dict[str, dict[str, object]] = field(default_factory=dict[str, dict[str, object]])
    _classifier: ComposioClassifier | None = None

    @property
    def classifier(self) -> ComposioClassifier:
        if self._classifier is None:
            self._classifier = ComposioClassifier(tools=self.client.tools, classes=self.classes)
        return self._classifier

    def serves(self, provider: str) -> bool:
        return bool(provider) and (not self.toolkits or provider in self.toolkits)

    def tool_class(self, slug: str, tool: object) -> ToolClass:
        self.classifier.remember(slug, tool)
        return self.classifier.classify(slug)

    async def tools(self, provider: str, *, refresh: bool = False) -> dict[str, object]:
        """Every tool of a toolkit (one listing page of up to `limit`), cached until `refresh`."""
        if not refresh and provider in self._tools:
            return self._tools[provider]
        listing = await asyncio.to_thread(
            self.client.tools.get_raw_composio_tools, toolkits=[provider], limit=self.limit
        )
        listed: dict[str, object] = {}
        for tool in listing:
            slug = _text(tool, "slug")
            if slug and toolkit_of(tool) in (None, provider):
                listed[slug] = tool
        self._tools[provider] = listed
        return listed

    async def _find_tool(self, provider: str, slug: str) -> object | None:
        tool = (await self.tools(provider)).get(slug)
        if tool is not None:
            return tool
        try:
            tool = await asyncio.to_thread(self.client.tools.get_raw_composio_tool_by_slug, slug)
        except Exception:  # noqa: BLE001 - an unknown slug is a 404, not a crash
            return None
        if tool is None or toolkit_of(tool) != provider:
            return None
        self._tools.setdefault(provider, {})[slug] = tool
        return tool

    async def execute(self, tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        if tool_name == PROVIDER_API:
            return await self._api(tool_input)
        if tool_name == PROVIDER_DOCS:
            return await self._docs(tool_input)
        return {"ok": False, "error": f"unknown tool {tool_name!r}; available tools: {PROVIDER_API}, {PROVIDER_DOCS}"}

    async def _api(self, tool_input: Mapping[str, Any]) -> dict[str, Any]:
        provider = str(tool_input.get("provider") or "").lower()
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

        if not self.serves(provider):
            known = ", ".join(self.toolkits) or "any toolkit slug"
            return respond(404, None, f"unknown provider {provider!r}; Composio toolkits: {known}")
        segments = [unquote(segment) for segment in path.strip("/").split("/")]
        try:
            if segments == ["tools"]:
                if method != "GET":
                    return respond(405, None, "tool discovery is GET /tools")
                tools = await self.tools(provider, refresh=True)
                listing = [
                    describe_tool(provider, slug, tool, self.tool_class(slug, tool)) for slug, tool in tools.items()
                ]
                return respond(200, {"tools": listing})
            if len(segments) == 2 and segments[0] == "tools":
                if method != "GET":
                    return respond(405, None, "tool description is GET /tools/{slug}")
                tool = await self._find_tool(provider, segments[1])
                if tool is None:
                    return respond(404, None, f"unknown tool {segments[1]!r} in toolkit {provider!r}")
                return respond(200, describe_tool(provider, segments[1], tool, self.tool_class(segments[1], tool)))
            if len(segments) == 3 and segments[0] == "tools" and segments[2] == "call":
                status, body, error, extra = await self._call(provider, method, segments[1], tool_input)
                return respond(status, body, error, **extra)
        except Exception as exc:  # noqa: BLE001 - transport failures are data, not crashes
            return respond(502, None, f"composio:{type(exc).__name__}: {str(exc)[:_MAX_ERROR_CHARS]}")
        routes = "GET /tools, GET /tools/{slug}, <method> /tools/{slug}/call"
        return respond(404, None, f"no Composio route {path!r}; routes: {routes}")

    async def _call(
        self, provider: str, method: str, slug: str, tool_input: Mapping[str, Any]
    ) -> tuple[int, object, str | None, dict[str, object]]:
        tool = await self._find_tool(provider, slug)
        if tool is None:
            return 404, None, f"unknown tool {slug!r} in toolkit {provider!r}", {}
        tool_class = self.tool_class(slug, tool)
        extra: dict[str, object] = {"tool": slug, "tool_class": tool_class}
        if method not in CLASS_METHODS[tool_class]:
            reason = (
                f"method_mismatch: tool {slug!r} is classified {tool_class}; call it with {_PRIMARY_METHOD[tool_class]}"
            )
            return 405, None, reason, extra
        arguments = _arguments(tool_input)
        if arguments is None:
            return 400, None, "the body must be a JSON object of tool arguments", extra
        response = await asyncio.to_thread(
            self.client.tools.execute,
            slug,
            arguments,
            connected_account_id=self.connected_account_id,
            user_id=self.user_id,
            version=self.version,
            dangerously_skip_version_check=self.dangerously_skip_version_check,
        )
        data = _field(response, "data")
        if _field(response, "successful") is True:
            return 200, data, None, extra
        error = str(_field(response, "error") or "tool reported an error")[:_MAX_ERROR_CHARS]
        if error.startswith(_REFUSAL_PREFIX):
            return 403, data, error, extra
        return 422, data, f"tool_error: {error}", extra

    async def _docs(self, tool_input: Mapping[str, Any]) -> dict[str, Any]:
        """`search` lists matching tools of the configured (or already listed) toolkits; `fetch` describes one."""
        action = str(tool_input.get("action") or "search")
        try:
            if action == "fetch":
                provider = str(tool_input.get("provider") or "").lower()
                slug = str(tool_input.get("tool") or tool_input.get("name") or "")
                if not self.serves(provider):
                    return {"ok": False, "error": f"unknown provider {provider!r}"}
                tool = await self._find_tool(provider, slug)
                if tool is None:
                    return {"ok": False, "error": f"unknown tool {slug!r} in toolkit {provider!r}"}
                return {"ok": True, "tool": describe_tool(provider, slug, tool, self.tool_class(slug, tool))}
            needle = str(tool_input.get("query") or "").casefold()
            wanted = str(tool_input.get("provider") or "").lower()
            providers = [wanted] if wanted else sorted(set(self.toolkits) | set(self._tools))
            results: list[dict[str, Any]] = []
            for provider in providers:
                if not self.serves(provider):
                    continue
                for slug, tool in (await self.tools(provider)).items():
                    haystack = f"{slug}\n{_text(tool, 'name') or ''}\n{_text(tool, 'description') or ''}".casefold()
                    if needle and needle not in haystack:
                        continue
                    described = describe_tool(provider, slug, tool, self.tool_class(slug, tool))
                    results.append(
                        {key: described[key] for key in ("provider", "name", "description", "class", "method", "path")}
                    )
            return {"ok": True, "results": results}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"composio:{type(exc).__name__}: {str(exc)[:_MAX_ERROR_CHARS]}"}


def composio_executor(
    client: ComposioLike,
    *,
    user_id: str | None = None,
    connected_account_id: str | None = None,
    toolkits: Sequence[str] = (),
    classes: Mapping[str, ToolClass] | None = None,
    version: str | None = None,
    dangerously_skip_version_check: bool | None = None,
    limit: int = 1000,
) -> ToolExecutor:
    """A Benchpress `ToolExecutor` over a Composio client; each provider name is a toolkit slug (`github`).

    `user_id` / `connected_account_id` / `version` / `dangerously_skip_version_check` are passed to every
    `tools.execute`. `toolkits` restricts the providers served (default: any toolkit slug). `classes`
    overrides a slug's class ahead of metadata tags and the slug heuristic. `limit` is the listing page size
    (the API allows up to 1000).
    """
    router = ComposioToolRouter(
        client=client,
        user_id=user_id,
        connected_account_id=connected_account_id,
        version=version,
        dangerously_skip_version_check=dangerously_skip_version_check,
        toolkits=tuple(toolkit.lower() for toolkit in toolkits),
        classes=dict(classes or {}),
        limit=limit,
    )
    return router.execute
