# Benchpress in front of Composio tools

Composio gives an agent hundreds of SaaS tools behind one `tools.execute` call. Benchpress puts
code-enforced authority in front of that call, two ways:

1. **`benchpress.shims.composio.guard_composio`**: every tool execution is classified (read / write /
   destructive) and checked against a guard policy **before Composio sees it**. A refused call never
   executes and comes back in the SDK's own response shape. Every call leaves a JSONL receipt.
2. **`benchpress.shims.composio.composio_executor`**: a Composio client becomes a Benchpress
   `ToolExecutor`, so `benchpress.wrap(...)` drives Composio tools through the mutation gate, with
   read-back and evidence-only status.

Verified against the `composio` Python SDK **0.21.1** (source read, not docs):
`Composio().tools.execute(slug, arguments, *, connected_account_id=None, user_id=None, text=None, version=None, dangerously_skip_version_check=None, modifiers=None, ...)`,
`tools.get_raw_composio_tool_by_slug(slug)`, `tools.get_raw_composio_tools(tools=None, search=None, toolkits=None, scopes=None, limit=None)`
and `tools.provider.set_execute_tool_fn(fn)`. The test suite asserts those signatures against the installed SDK.

## Install

```bash
pip install "benchpress-agent[composio]"   # pulls composio>=0.21.1; plain benchpress-agent never imports it
```

## 1. The guard

```python
from composio import Composio
from benchpress.shims.composio import guard_composio

policy = {
    "rules": [
        {"tool": "GITHUB_CREATE_AN_ISSUE", "arguments": {"repo": "sandbox-.*"}, "max_calls": 5},
        {"tool": "GMAIL_*", "effect": "deny", "reason": "mail is off limits for this agent"},
    ],
    "receipts": "composio-receipts.jsonl",
}
composio = guard_composio(Composio(), policy)

composio.tools.execute("GITHUB_LIST_REPOSITORY_ISSUES", {"owner": "o", "repo": "r"}, user_id="user-1")  # read: runs
composio.tools.execute("GITHUB_CREATE_AN_ISSUE", {"owner": "o", "repo": "sandbox-1", "title": "t"}, user_id="user-1")  # allowed
composio.tools.execute("GITHUB_DELETE_A_REPOSITORY", {"owner": "o", "repo": "r"}, user_id="user-1")  # refused
```

The refusal is not an exception. It is the SDK's `ToolExecutionResponse` shape, so agent loops that
already handle `successful: False` keep working and the model sees why:

```python
{"data": {}, "successful": False,
 "error": "benchpress refused 'GITHUB_DELETE_A_REPOSITORY' [no_allow_rule]: 'GITHUB_DELETE_A_REPOSITORY' is classified destructive and no policy rule allows it"}
```

### It guards the client in place

By default (`in_place=True`) the guard rebinds both SDK entry points on the client you pass:

- `client.tools.execute`, so code holding the original client cannot bypass the policy;
- the provider's injected execute function (`provider.set_execute_tool_fn`), the path
  `composio.provider.handle_tool_calls(...)` and agentic-provider tools (OpenAI Agents, LangChain, ...)
  use. The SDK's own `dangerously_skip_version_check=True` binding for that path is preserved.

**Guard before you fetch tools.** Agentic providers bind `partial(client.tools.execute, ...)` when
`client.tools.get(user_id, ...)` runs (SDK `Tools._wrap_execute_tool`). Tools fetched after
`guard_composio` are guarded. Tool objects fetched before it still hold the unguarded method.

The returned `GuardedComposio` exposes the same `tools.execute(...)` and forwards every other attribute
(`toolkits`, `connected_accounts`, `tools.get(...)`, ...) to the client. `guarded.receipts` holds the
receipt lines in memory. Pass `in_place=False` to leave the client untouched and guard only calls made
through the returned wrapper.

## 2. The executor

```python
import benchpress
from composio import Composio
from benchpress.shims.composio import composio_executor

execute = composio_executor(Composio(), user_id="user-1", toolkits=["github"])
agent = benchpress.wrap("claude-sonnet-4-5", execute, providers=["github"])
result = await agent.run("Label every open issue that mentions the outage as incident")
```

Each provider name is a Composio toolkit slug. The loop's `provider_api` tool maps onto Composio like this:

| Request | Composio | Notes |
|---|---|---|
| `GET /tools` | `get_raw_composio_tools(toolkits=[provider], limit=...)` | every tool with `class`, `method`, `path`, `tags`, schemas |
| `GET /tools/{slug}` | from the listing, else `get_raw_composio_tool_by_slug` | one tool's description |
| `GET /tools/{slug}/call` | `tools.execute` | **read** tools only |
| `POST`, `PUT`, `PATCH /tools/{slug}/call` | `tools.execute` | **write** tools only |
| `DELETE /tools/{slug}/call` | `tools.execute` | **destructive** tools only |

Arguments are the JSON body (an object); a GET with no body takes arguments from `query`. `user_id`,
`connected_account_id`, `version` and `dangerously_skip_version_check` given to `composio_executor`
are passed to every execution. `provider_docs` works too: `{"action": "search", "query": "issue"}`
over the configured toolkits, `{"action": "fetch", "provider": "github", "tool": "GITHUB_CREATE_AN_ISSUE"}`.

**Why methods are bound to classes.** The Benchpress gate inspects every non-GET action and never a GET,
so a GET must never reach a tool that writes. A method that does not match the tool's class gets a 405
before Composio sees anything. Destructive tools map to DELETE, which the gate refuses unconditionally,
so **a destructive Composio tool cannot run through the Benchpress loop**. A slug from another toolkit
is a 404 through this provider.

**Results** use the gateway shape the tool bus reads, `{ok, status_code, body, error}`: `successful`
is 200 with `data` as the body; `successful: False` is 422 with `tool_error: ...`; a refusal from a
guarded client (both halves together) is 403; an SDK exception is 502.

The two halves compose. Guard the client, then build the executor from it: the gate governs the loop and
the guard policy still refuses anything outside its rules.

## Classification

In order, first match wins:

1. `classes={"GITHUB_SYNC_FORK": "write"}` passed to `guard_composio` / `composio_executor` (by slug);
2. the policy file's `classes` (guard only);
3. tool metadata. The SDK types `tags` as free-form strings. A `destructiveHint` tag makes a tool
   destructive and a `readOnlyHint` tag makes it read (matched case- and punctuation-insensitively),
   except that a destructive verb in the slug always wins over a read-only tag;
4. the shared name heuristic `classify_tool_name` (the same function the OpenAI Agents shim uses),
   applied to the slug **without its toolkit prefix**: `GITHUB_LIST_REPOSITORY_ISSUES` is judged as
   `LIST_REPOSITORY_ISSUES`, so it is a read. A destructive verb anywhere (`delete`, `remove`, `cancel`,
   `refund`, ...) is destructive, a leading read verb (`get`, `list`, `search`, `fetch`, ...) is read,
   anything else is write.

The toolkit prefix comes from the tool's metadata (`tool.toolkit.slug`). The guard looks metadata up
once per slug with `get_raw_composio_tool_by_slug`, the same lookup the SDK's own `execute` makes, and
caches it. With `metadata=False`, or when the lookup fails, only the first `_` token is dropped, which
for a multi-word toolkit leaves a non-verb in front and errs toward write, never toward read.

## Policy

The policy format is the one `benchpress mcp-guard` and the OpenAI Agents shim use
(`benchpress.shims.guard_policy`), so one JSON file can govern all three. See
[docs/MCP.md, Policy file](./MCP.md#policy-file) for the full format and
[docs/OPENAI-AGENTS.md](./OPENAI-AGENTS.md#policy) for the decision order. In short: rules in order by
glob over the slug (case-sensitive; Composio slugs are upper case, so write `GITHUB_*`), `deny` refuses,
`allow` needs every constrained argument to fully match its regex and counts against `max_calls`,
destructive tools stay refused unless the rule sets `allow_destructive` (even `{"tool": "*"}` never
allows one), reads follow `reads` (default `allow`), and everything else is refused. Arguments that are
not a mapping are refused (`invalid_arguments`).

## Receipts

One JSONL line per guarded call. Argument values are never stored, only a digest:

```json
{"args_digest": "sha256:1b6f...", "class": "write", "decision": "allow", "latency_ms": 412.7, "policy_rule": 0,
 "reason": "allowed by policy rule 0", "rule": "allow_rule", "tool": "GITHUB_CREATE_AN_ISSUE",
 "ts": "2026-09-13T22:04:05+00:00", "upstream_error": false}
```

`upstream_error` is `true` when the SDK raised (the exception still propagates) or returned
`successful: False`, `false` on success, and `null` for a refusal. Location: the `receipts` argument,
else the policy's `receipts` (relative to the policy file when the policy is a path, else the working
directory), else `composio-guard-receipts.jsonl` there. `receipts=False` keeps lines in memory only.

## Testing without an account

`tests/composio_fakes.py` is a client double with the verified signatures, returning the SDK's own
`composio.client.types.Tool` models built with `model_construct`. `tests/test_composio_guard.py` and
`tests/test_composio_executor.py` cover classification, every policy outcome, in-place rebinding of both
entry points, receipts, every executor route, and the tool bus plus gate driving Composio tools. None of
them touch the network.

## Limits

- **Tool Router sessions are not guarded.** A provider executing against a Tool Router session
  (`session.execute(...)`) and `tools.proxy(...)` (raw HTTP through a connected account) do not go
  through `tools.execute`. Guard direct `user_id` execution, or put the session's MCP URL behind
  `benchpress mcp-guard`.
- **Agentic tool objects fetched before guarding stay unguarded** (see above). Call `guard_composio`
  first.
- **Tag vocabulary is not guaranteed.** The SDK types `tags` as `List[str]` and documents no hint
  vocabulary. The hint tags are honoured when present. The shim was built without a Composio account,
  so the live catalog's tags were not inspected. Declare `classes` for anything that matters.
- **Classification never sees behaviour.** `GMAIL_MOVE_TO_TRASH` is a write to the heuristic, not
  destructive, and `..._SEND_...` is a write (refused by default, which is the point). Declare `classes`
  for tools whose names understate what they do.
- **No read-back in the guard.** The guard decides and records. Verification is the Benchpress loop's job
  (the executor plus `benchpress.wrap`). Policy packs are not wired into the Composio guard.
- **Metadata lookups are Composio API calls** (one per slug, cached for the guard's life, the same call
  the SDK's `execute` already makes). Discovery lists one page of up to `limit` tools (API maximum
  1000); tools beyond it are still found by slug.
- Guarding the same client twice stacks both guards: both must allow. `max_calls` counters live as long
  as the guard. Argument constraints see top-level arguments only. The SDK is synchronous; the executor
  runs it in worker threads.
