# Benchpress in front of MCP tools

Two ways to put Benchpress between an agent and tools served over the Model Context Protocol:

1. **`benchpress.shims.mcp.mcp_executor`**: MCP sessions become a Benchpress `ToolExecutor`, so the
   tool bus, the mutation gate and the receipts of the Benchpress loop govern MCP tools.
2. **`benchpress mcp-guard`**: an MCP proxy for any MCP client (Claude Desktop, Cursor, your own
   agent), over stdio or streamable HTTP. It re-exposes an upstream server's tools and refuses writes in
   code unless a policy allows them.

The gateway (`benchpress serve`) is also an MCP server in its own right, with verified writes as tools:
see [section 3](#3-pointing-an-mcp-client-at-the-gateway).

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

### Guard over streamable HTTP

```bash
benchpress mcp-guard --policy guard.json --http 127.0.0.1:8788 -- <upstream MCP server command...>
```

The upstream still runs as a stdio subprocess under the same policy and receipts, but clients connect to
`http://127.0.0.1:8788/mcp` over streamable HTTP instead of spawning the guard. Use it for a client that
only speaks HTTP, or to put several local clients behind one guard. They then share its `max_calls`
counters, which count per guard process.

- **The guard has no auth.** `--http` expects a loopback host (`127.0.0.1`, `localhost`, `[::1]`) and a
  port from 1 to 65535. Any other host exits 2 with
  `the guard has no auth; bind to loopback or pass --allow-remote`. With `--allow-remote` the guard serves
  anyway and prints a warning to stderr, because anyone who can reach the port can call every tool the
  policy allows. Put an authenticating proxy in front of a remote guard.
- **Host checks.** On a loopback bind the guard keeps the MCP SDK's DNS-rebinding protection. The `Host`
  header must be `127.0.0.1:<port>`, `localhost:<port>`, `[::1]:<port>` or the bound host, and a browser
  `Origin`, when sent, must be `http://` plus one of those. Anything else gets 421 (Host) or 403 (Origin).
  A remote bind checks neither header.

## 3. Pointing an MCP client at the gateway

`benchpress serve` serves MCP over streamable HTTP at `/mcp/`, with the trailing slash. The tools call the
same service as the HTTP API, so an MCP write is gated, executed, read back, receipted and counted in
`/metrics` exactly like `POST /v1/execute`, and its receipt appears in `GET /v1/receipts`.

```bash
claude mcp add --transport http benchpress http://127.0.0.1:8787/mcp/ --header "Authorization: Bearer bp_…"
```

Any streamable-HTTP client works the same way: the URL `http://HOST:PORT/mcp/` and an
`Authorization: Bearer <API key>` header, using a key from `benchpress workspace create`/`key` or the
bootstrap key `benchpress serve` prints. With the Python SDK, headers go on the HTTP client:

```python
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

async with (
    httpx2.AsyncClient(headers={"Authorization": "Bearer bp_…"}) as http,
    Client(streamable_http_client("http://127.0.0.1:8787/mcp/", http_client=http)) as gateway,
):
    result = await gateway.call_tool("verified_write", {"action": action, "context": {"user_prompt": prompt}})
```

**Auth** follows the HTTP API's rules and uses the same key check. Under `auth = "api_key"` (the default),
every request to `/mcp/` needs a valid key. Without one the endpoint answers `401` with
`WWW-Authenticate: Bearer` before any MCP session exists, so an anonymous client cannot even connect. Each
tool call then draws once on the key's `requests_per_minute`, the same budget its HTTP requests use, however
many HTTP requests the call takes. A rate-limited key is a tool error (`rate limit exceeded`). Under
`auth = "none"` (loopback only), the endpoint is open and every call acts as the `default` workspace.

| Tool | Arguments | Returns |
|---|---|---|
| `verified_write` | `action`, plus exactly one of `session_id` or `context` | the `POST /v1/execute` body: `status` (`verified`, `mismatch`, `unverified`, `failed`, `refused`, `needs_approval`), `session_id`, `verdict`, `status_code`, `evidence`, `receipt_id`, `approval_id` |
| `read` | `provider`, `path`, `query` (optional) | `{ok, status_code, body, error}` from one provider GET. Reads are not gated, but a control-plane path is refused. |
| `explain_refusal` | same as `verified_write` | `{allowed, rule, reason, help}`. It never executes, claims or receipts anything, and an inline `context` creates no session. |

An inline `context` on `verified_write` creates a one-shot session, exactly as it does over HTTP.

**A refusal is data, not an error.** A refused write returns `status: "refused"`, with the rule and reason in
`verdict`. Tool errors (`isError: true`) are for calls that cannot be judged: a rate-limited key, invalid
input, an unknown session or a control-plane read. The error text is the message. For invalid input it names
each field and never echoes the rejected value, since an action's headers might carry a credential.

`help` is one plain sentence per gate rule (`benchpress.gateway.mcp_server.RULE_HELP`). Every policy pack
rule (`pack:<pack>.<rule>`) gets the generic `policy_pack` sentence, and its `reason` names the pack rule.

**Host checks.** When the gateway's `host` is `127.0.0.1`, `localhost` or `::1`, the MCP endpoint keeps the
MCP SDK's DNS-rebinding protection. The `Host` header must be `127.0.0.1:<port>`, `localhost:<port>` or
`[::1]:<port>`, and a browser `Origin`, when sent, must be `http://` plus one of those. On any other host
neither header is checked.

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
- The guard's upstream runs over stdio only (the transport desktop clients use). Clients can reach the
  guard over stdio or streamable HTTP.
