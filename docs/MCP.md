# Benchpress in front of MCP tools

Two ways to put Benchpress between an agent and tools served over the Model Context Protocol:

1. **`benchpress.shims.mcp.mcp_executor`**: MCP sessions become a Benchpress `ToolExecutor`, so the
   tool bus, the mutation gate and the receipts of the Benchpress loop govern MCP tools.
2. **`benchpress mcp-guard`**: a stdio MCP proxy for any MCP client (Claude Desktop, Cursor, your own
   agent). It re-exposes an upstream server's tools and refuses writes in code unless a policy allows them.

## Install

```bash
pip install 'benchpress-agent[mcp]'     # pulls mcp>=2.2.0; plain `benchpress-agent` never imports it
```

## 1. The executor

```python
from mcp import Client, StdioServerParameters
from benchpress.shims.mcp import mcp_executor
from benchpress.tools import ToolBus

async with Client(StdioServerParameters(command="my-tickets-server")) as tickets:
    execute = mcp_executor({"tickets": tickets})          # provider name -> entered Client or ClientSession
    result = await execute("provider_api", {"provider": "tickets", "method": "GET", "path": "/tools"})
```

The loop emits one harness-shaped tool, `provider_api` (`provider, method, path, query?, body?`). The
executor maps it onto MCP like this:

| Request | MCP | Notes |
|---|---|---|
| `GET /tools` | `tools/list` (all pages) | every tool with `class`, `method`, `path`, schemas, annotations |
| `GET /tools/{name}` | from the listing | one tool's description |
| `GET /tools/{name}/call` | `tools/call` | **read** tools only |
| `POST`, `PUT`, `PATCH /tools/{name}/call` | `tools/call` | **write** tools only |
| `DELETE /tools/{name}/call` | `tools/call` | **destructive** tools only |

Arguments are the JSON body (an object). A GET with no body takes string arguments from `query`.
`provider_docs` works too: `{"action": "search", "query": "ticket"}` lists matching tools across
providers, `{"action": "fetch", "provider": "tickets", "tool": "update_ticket"}` describes one.

**Classification.** `readOnlyHint: true` is read. `destructiveHint: false` is write. Everything else is
destructive, which is the MCP specification's own default for a tool without annotations
(`readOnlyHint` defaults to false, `destructiveHint` to true). Pass
`classes={"tickets/sync_all": "write"}` (or a bare tool name) to override annotations.

**Why methods are bound to classes.** The Benchpress gate inspects every non-GET action and never a
GET, so a GET must never reach a tool that writes. A method that does not match the tool's class
gets a 405 before the server sees anything. Destructive tools map to DELETE, and the gate refuses
DELETE unconditionally, so **a destructive MCP tool cannot run through the Benchpress loop**.

**Results** come back in the gateway shape the tool bus reads: `{ok, status_code, body, error}`.
`body` is the tool's `structuredContent` when present, else the parsed JSON of a single text block,
else `{"text": ..., "content": [...]}`. `isError` becomes status 422 with the tool's text in `error`.
Unknown provider, tool or route is 404. A transport failure is 502.

## 2. `benchpress mcp-guard`

```bash
benchpress mcp-guard --policy guard.json [--receipts receipts.jsonl] -- <upstream MCP server command...>
```

The guard starts the upstream as a subprocess (it inherits the guard's environment), lists its tools
unchanged, and decides each `tools/call` before forwarding it:

1. Unknown tool: refused (`unknown_tool`).
2. Rules are checked **in order**, matched by tool-name glob:
   - `deny`: refused (`deny_rule`).
   - `allow`: applies only if every constrained argument fully matches its regex (otherwise the next
     rule is tried; `arguments_mismatch` if none applies). Then it enforces `max_calls`
     (`max_calls`). Destructive tools stay refused unless the rule sets `allow_destructive`
     (`destructive_default_deny`, so even `{"tool": "*"}` never allows a destructive tool).
3. No applicable allow rule: read tools follow `reads` (default `allow`). Anything else is refused (`no_allow_rule`).

A refusal is an MCP tool result with `isError: true` and text such as
`benchpress mcp-guard refused 'delete_ticket' [no_allow_rule]: 'delete_ticket' is classified destructive and no policy rule allows it`.
The calling model sees why and can adjust.

### Policy file

```json
{
  "reads": "allow",
  "classes": { "sync_everything": "write" },
  "rules": [
    { "tool": "admin_*", "effect": "deny", "reason": "admin tools are off limits for agents" },
    { "tool": "update_ticket", "arguments": { "status": "pending|resolved", "ticket_id": "T-\\d+" }, "max_calls": 20 },
    { "tool": "add_comment", "max_calls": 50 },
    { "tool": "close_duplicate", "allow_destructive": true, "max_calls": 5 }
  ],
  "receipts": "mcp-guard-receipts.jsonl"
}
```

The policy model lives in `benchpress.shims.guard_policy` and is shared with the OpenAI Agents SDK
shim ([docs/OPENAI-AGENTS.md](./OPENAI-AGENTS.md)), so one file can govern both.

`classes` overrides annotations (use it for servers you do not control; annotations are hints written
by the server). Argument regexes use Python `re.fullmatch` against the argument's string value, or its
canonical JSON for non-strings. Unknown keys and invalid regexes fail at startup.

### Receipts

Every call, allowed or refused, appends one JSONL line. Argument values are never stored, only a digest:

```json
{"args_digest": "sha256:9f2c...", "class": "write", "decision": "allow", "latency_ms": 41.7, "policy_rule": 1,
 "reason": "allowed by policy rule 1", "rule": "allow_rule", "tool": "update_ticket", "ts": "2026-09-13T21:04:05+00:00",
 "upstream_error": false}
```

Default location: `--receipts`, else the policy's `receipts` (relative to the policy file), else
`mcp-guard-receipts.jsonl` next to the policy file.

### Claude Desktop / Cursor

Wrap the server command you already have. Claude Desktop (`claude_desktop_config.json`) and Cursor
(`~/.cursor/mcp.json`) use the same `mcpServers` shape:

```json
{
  "mcpServers": {
    "tickets-guarded": {
      "command": "benchpress",
      "args": ["mcp-guard", "--policy", "/Users/me/guards/tickets.json", "--", "npx", "-y", "@acme/tickets-mcp"],
      "env": { "TICKETS_API_TOKEN": "..." }
    }
  }
}
```

Use absolute paths: desktop clients start servers from an unpredictable working directory.

## Limits

- **Annotations are hints.** An upstream that marks a writing tool `readOnlyHint: true` is treated
  as read unless `classes` says otherwise. The guard enforces the policy, not the server's honesty.
- **No read-back in the guard.** It forwards an allowed write and records it; it does not verify
  the resulting state, even when the upstream offers a read tool for the entity. Verification
  belongs to the Benchpress loop (`mcp_executor` + `run_trial`).
- **`run_trial` needs a playbook per provider.** The shipped playbooks speak the Slack, Gmail,
  HubSpot and Stripe HTTP shapes. The executor gives an MCP provider a transport, the gate and
  receipts. Candidate search and write construction for it need a small playbook that builds
  `Action`s against `/tools/{name}/call`.
- `max_calls` counters live for one guard process and reset on restart.
- Argument constraints see top-level arguments only.
- Tools only: upstream resources and prompts are not proxied. `tools/list_changed` notifications are
  not forwarded, but every `tools/list` re-reads the upstream.
- Upstream over stdio only (the transport desktop clients use).
