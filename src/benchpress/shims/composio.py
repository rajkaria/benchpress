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

Verified against the `composio` Python SDK 0.21.1 (`Composio().tools`: `execute`,
`get_raw_composio_tool_by_slug`, `get_raw_composio_tools`, `provider.set_execute_tool_fn`).
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, cast, runtime_checkable

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

__all__ = [
    "COMPOSIO_SDK_VERSION",
    "DEFAULT_RECEIPTS_NAME",
    "ComposioClassifier",
    "ComposioGuard",
    "ComposioLike",
    "ComposioToolsLike",
    "GuardedComposio",
    "GuardedTools",
    "classify_composio_tool",
    "guard_composio",
    "refusal_response",
]

DEFAULT_RECEIPTS_NAME = "composio-guard-receipts.jsonl"

_READ_ONLY_TAGS = frozenset({"readonlyhint", "readonly"})
_DESTRUCTIVE_TAGS = frozenset({"destructivehint", "destructive"})


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


type ExecuteFn = Callable[..., Any]


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
