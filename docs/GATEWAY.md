<p align="center">
  <img src="img/banner.svg" alt="Benchpress: the reliability layer for AI agents with write access" width="100%">
</p>

# The Benchpress gateway

## 1. What the gateway is

The gateway puts `VerifiedWrite` — gate the write, run it, read it back, keep the evidence — behind a long-lived
server that many callers and many replicas share. One HTTP surface (`/v1/execute`, sessions, receipts, approvals,
policies) and one MCP surface (`/mcp/`, three tools) call the same `GatewayService`, so an HTTP client and an MCP
client that send the same write get the same gate verdict, the same read-back and the same receipt line. It is the
team door from the README's three doors: `pip install benchpress-agent` for one developer in-process, `docker run
ghcr.io/rajkaria/benchpress` or `benchpress serve` for a team that wants a gateway every agent points at.

## 2. Quickstart

```bash
pip install --pre "benchpress-agent[server]"
benchpress serve
```

On the first start against an empty store, `serve` creates a `default` workspace and a bootstrap API key. If
`BENCHPRESS_BOOTSTRAP_KEY` is unset or empty (an empty value counts as unset), one is generated and printed **once**
to stderr — which is `docker logs` under the container — and never again on a later start against the same store. A
key you provide yourself must start with `bp_` and be at least 32 characters, checked before anything is written; a
key that fails that check stops `serve` with a config error (exit 2) rather than silently falling back to a
generated one.

```
benchpress serve: bootstrapped workspace 'default'; API key (shown once): bp_Ax7...
benchpress gateway: http://127.0.0.1:8787 (console /, API /v1, MCP /mcp/)
```

A first request (any HTTP client works; shown here with `httpx`):

```python
import httpx

client = httpx.Client(base_url="http://127.0.0.1:8787", headers={"Authorization": "Bearer bp_Ax7..."})
session = client.post("/v1/sessions", json={
    "context": {
        "user_prompt": "Rivermill asked for renewal notices to go to ap@rivermill.example.",
        "providers": ["hubspot"],
        "protected": {"ids": ["702"]},
        "write_scope": ["hubspot"],
    },
}).json()

result = client.post("/v1/execute", json={
    "session_id": session["session_id"],
    "action": {
        "id": "w1", "kind": "update", "provider": "hubspot", "method": "PATCH",
        "path": "/crm/v3/objects/companies/701",
        "body": {"properties": {"email": "ap@rivermill.example"}}, "fields": ["email"],
        "readback": {"path": "/crm/v3/objects/companies/701", "field_path": "email"},
    },
}).json()
print(result["status"])   # refused | failed | unverified | verified | mismatch | needs_approval
```

**Container:**

```bash
docker run -p 8787:8787 -v benchpress-data:/data -e BENCHPRESS_BOOTSTRAP_KEY=bp_replace-with-a-real-32-char-plus-secret ghcr.io/rajkaria/benchpress
```

`docker-compose.yml` runs the same image with a named volume:

```bash
docker compose up
# with Postgres instead of the default SQLite volume:
BENCHPRESS_STORE=postgresql://benchpress:benchpress@postgres:5432/benchpress docker compose --profile postgres up
```

## 3. Sessions and context ownership

The gateway never infers what a write is allowed to touch — the caller states it, once, as a session. This is
[RFC 0001](rfcs/0001-gateway-sessions.md) in full; the short version:

- `POST /v1/sessions` persists `context` (`user_prompt`, `providers`, `protected` ids/names/domains/emails,
  `targets`, `write_scope`, `forbidden`) and returns a `session_id` any replica can rebuild a writer from.
- `POST /v1/execute` takes exactly one of `session_id` or an inline `context`. An **inline `context` creates a
  one-shot session** with the default `session` idempotency scope, so a caller who needs replay protection across
  more than one call creates a session first (optionally with `idempotency_scope = "workspace"`) and sends its
  `session_id` on every subsequent write.
- Every session's writer shares one SQL-backed idempotency store, so a byte-identical write already done, or still
  in flight on another replica, is refused with rule `idempotency` rather than duplicated or queued.
- A session's gate runs with `allow_unplanned=True` (the gateway has no plan to check membership against) and
  applies every workspace policy pack on top of anything loaded from `policy_dir` at startup.

## 4. HTTP API

Every route below except `/healthz` needs `Authorization: Bearer bp_...` when `auth = "api_key"` (the default); a
missing or unknown key is `401` with `WWW-Authenticate: Bearer`, and a key over `requests_per_minute` is `429` with
`Retry-After: 1`. Under `auth = "none"` (loopback hosts only) every request acts as the `default` workspace and
nothing is rate-limited. A body over `max_body_bytes` is `413` before any route runs.

| Method & path | Body | Response |
|---|---|---|
| `GET /healthz` | — | `{status, version, store}`; `200` when the store answers, `503` (`store: "error"`) otherwise. No auth. |
| `GET /v1/meta` | — | `{mode: "gateway", version, auth}` |
| `POST /v1/sessions` | `{session_id?, context, idempotency_scope?}` | `201` `{session_id, idempotency_scope}`; a taken `session_id` is `409` |
| `POST /v1/execute` | `{action, session_id \| context}` (exactly one) | `ExecuteResponse`: `{status, session_id, verdict, status_code, evidence, receipt_id, approval_id}`. `202` when `status` is `needs_approval`, else `200`. `422` for a `GET` action, a credential-shaped header, or neither/both of `session_id`/`context` |
| `GET /v1/policies` | — | `{bundled, directory, workspace}`: bundled pack names, `policy_dir` pack names, and this workspace's stored packs with `updated_at` |
| `PUT /v1/policies/{name}` | raw YAML body | `{name, updated_at}`; `422` if the pack's own `name:` does not match the path |
| `DELETE /v1/policies/{name}` | — | `204`; `404` if no such workspace policy |
| `GET /v1/approvals` | query `?status=` | `list[ApprovalView]` |
| `GET /v1/approvals/{approval_id}` | — | `ApprovalView`; `404` outside this workspace |
| `POST /v1/approvals/{approval_id}` | `{decision: "approve"\|"deny", note?}` | `ExecuteResponse` — approving resumes and receipts the parked write; denying returns `status: "denied"` with no execution; `409` if already resolved or expired |
| `GET /v1/receipts` | query: `customer, provider, rule, status, session, event, limit (1-500), before` | `{receipts: [ReceiptSummary...], next_before}`, newest first |
| `GET /v1/receipts/{receipt_id}` | — | `ReceiptDetail` (`{id, kind, payload, html}`); `404` outside this workspace |
| `GET /v1/receipts/{receipt_id}/html` | — | a self-contained HTML page, only for `kind: "run"` receipts; `404` otherwise (gateway write receipts have no HTML page) |
| `GET /metrics` | — | Prometheus text exposition; gated by `metrics_auth`, independently of `auth` |

`/mcp/` (three MCP tools) is covered in [section 6](#6-mcp), and the console at `/` in [section 7](#7-console-and-benchpress-ui).
FastAPI itself also serves `/docs`, `/redoc` and `/openapi.json` — **these are not authenticated**; `/healthz` is not
the only unauthenticated route.

## 5. Approvals

A write the gate allows can still be parked for a person, when it matches an `[[approvals.rules]]` entry
(`name`, `provider` glob, `methods`, `path` glob, `classes` — the same action-class labels `benchpress gate check`
reports, e.g. `create_charge`, `send_email`). Rules are checked in file order; the first full match wins. Parking:

- Sends nothing to the provider and takes no idempotency claim.
- Appends an `approval_requested` receipt line and returns `ExecuteResponse` with `status: "needs_approval"` and
  `approval_id` set (`202`).
- Best-effort POSTs a webhook, if `approval_webhook_url` is set, with the request signed when
  `approval_webhook_secret` is also set:

  ```json
  {"event": "approval_requested", "approval": {"id": "apr_...", "session_id": "...", "rule": "...", "status": "pending", "requested_at": "...", "expires_at": "...", "action": {...}, "fingerprint": "..."}, "workspace": "acme"}
  ```

  Headers: `Content-Type: application/json`, `X-Benchpress-Event: approval_requested`, and
  `X-Benchpress-Signature: sha256=<hex>`, an HMAC-SHA256 of the exact request body over the secret. Verify it in
  Python:

  ```python
  import hashlib, hmac

  def verify(body: bytes, header: str, secret: str) -> bool:
      expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
      return hmac.compare_digest(expected, header)
  ```

  A webhook failure (bad URL, timeout, non-2xx) is logged and never fails the park.
- `POST /v1/approvals/{id}` with `{"decision": "approve"}` passes the write through the gate and the idempotency
  claim **again**, executes it and appends the real write receipt (with `approval_id` set); `{"decision": "deny"}`
  appends `approval_resolved` and executes nothing. Either way an `approval_resolved` receipt line is appended, and
  the actor recorded is the API key's name, or `anonymous` under `auth = "none"`.
- An approval past `approval_ttl_seconds` (default 3600) is closed `expired` — resolved automatically by a sweep
  that runs roughly every 30 seconds whenever any `approval_rules` are configured, or lazily the next time it is
  looked up or resolved.

## 6. MCP

`benchpress serve` mounts the gateway's MCP server at `/mcp/` (streamable HTTP, trailing slash) over the same
`GatewayService` the HTTP routes call, so an MCP write is gated, executed, read back, receipted and counted exactly
like `POST /v1/execute` — its receipt shows up in `GET /v1/receipts` and its metrics are labelled `route="/mcp"`
(the write counters are the same `benchpress_writes_total`/`benchpress_refusals_total` series `/v1/execute` uses).

| Tool | Arguments | Returns |
|---|---|---|
| `verified_write` | `action`, plus exactly one of `session_id` or `context` | the `POST /v1/execute` body |
| `read` | `provider`, `path`, `query?` | `{ok, status_code, body, error}` from one provider `GET`; not gated, but a control-plane path is refused |
| `explain_refusal` | same as `verified_write` | `{allowed, rule, reason, help}`; never executes, claims or receipts anything |

`verified_write` is annotated **destructive** (`destructiveHint: true`) — a client that auto-approves
non-destructive tools will not wave it through. Every tool call carries a rejection or invalid-input error as a tool
error whose text is prefixed by the MCP SDK: `Error executing tool <name>: <message>`.

**Auth is one key check, shared with HTTP.** Under `auth = "api_key"`, a request to `/mcp/`
without a valid key is refused `401` **before an MCP session is created** — so `tools/list` needs the key too, not
only a tool call. Each tool call then draws once on the key's `requests_per_minute`, the same budget its HTTP
requests use; a rate-limited key surfaces as the tool error `rate limit exceeded`. Under `auth = "none"`
(loopback only) the endpoint is open.

`benchpress mcp-guard` — the stdio MCP proxy that puts a policy in front of any *other* MCP server — also has a
streamable-HTTP mode, `mcp-guard --policy guard.json --http HOST:PORT -- <upstream ...>`, for a client that only
speaks HTTP. Full policy format, the guard's `--http`/`--allow-remote` rules, the gateway's Python client example
and its exact Host/Origin behavior are in [docs/MCP.md](MCP.md#3-pointing-an-mcp-client-at-the-gateway). In short:
the MCP SDK's DNS-rebinding protection stays on for a loopback bind, so a gateway on `127.0.0.1` sitting behind a
same-host reverse proxy that forwards a public `Host` header gets `421` on `/mcp/` while `/v1` routes still work —
bind the gateway to the address the proxy actually connects to, or put the proxy on a different host.

## 7. Console and `benchpress ui`

The gateway serves a receipts list and detail console (React) at `/` whenever `console = true` (the default);
`packages/console` is its source, built into the installed package. The console prompts for an API key on a `401`
and stores it in the browser's `sessionStorage`, clearing it on any later `401`.

```
benchpress ui --dir runs/demo --port 8788
```

`benchpress ui` serves the same console read-only, over on-disk receipt files (JSONL write/guard logs and
`receipt.json` run receipts under `--dir`) instead of the gateway's store — no `Store`, no auth, no metrics, no body
cap, and it answers only `GET` requests. It refuses to bind anywhere but loopback (`is_loopback`), because it has no
authentication of any kind.

## 8. Configuration reference

`benchpress.toml`, environment variables and `serve`'s CLI flags layer over the dataclass defaults, lowest to
highest precedence: defaults < `benchpress.toml` < environment < explicit overrides.

| Setting | `benchpress.toml` key | Env var | Default |
|---|---|---|---|
| `host` | `[server].host` | `BENCHPRESS_HOST` | `127.0.0.1` |
| `port` | `[server].port` | `BENCHPRESS_PORT` | `8787` |
| `store` | `[server].store` | `BENCHPRESS_STORE` | `sqlite:///benchpress.db` |
| `policy_dir` | `[server].policy_dir` (relative to the config file) | `BENCHPRESS_POLICY_DIR` | none |
| `auth` | `[server].auth` (`"api_key"` \| `"none"`) | `BENCHPRESS_AUTH` | `api_key` |
| `metrics_auth` | `[server].metrics_auth` (`"api_key"` \| `"none"`) | — | `api_key` |
| `console` | `[server].console` | — | `true` |
| `requests_per_minute` | `[limits].requests_per_minute` | — | `600` |
| `max_body_bytes` | `[limits].max_body_bytes` | — | `1048576` (1 MiB) |
| `idempotency_lease_seconds` | `[limits].idempotency_lease_seconds` | — | `300.0` |
| `approval_ttl_seconds` | `[approvals].ttl_seconds` | — | `3600` |
| `approval_webhook_url` | `[approvals].webhook_url` | — | none |
| `approval_webhook_secret` | `[approvals].webhook_secret`, an `env:NAME` reference | — | none |
| `approval_rules` | `[[approvals.rules]]` (`name`, `provider`, `methods`, `path`, `classes`) | — | none |
| `upstreams` | `[[upstreams]]` (`provider`, `base_url`, `token` as `env:NAME`, `role`) | — | none |
| `sessions_cache` | not yet exposed in `benchpress.toml` or the CLI; pass `overrides={"sessions_cache": N}` to `load_settings`, or construct `Settings(sessions_cache=...)` directly, if you embed the gateway | — | `1024` |

`auth`/`metrics_auth` of `"none"` is only accepted when `host` is loopback (`127.0.0.1`, `localhost`, `::1`).
`requests_per_minute`, `max_body_bytes`, `idempotency_lease_seconds` and `approval_ttl_seconds` must be positive.
Every secret-shaped value (`approval_webhook_secret`, an upstream `token`) must be an `env:NAME` reference — never
written inline — and is never logged or written to a receipt.

```toml
[server]
host = "127.0.0.1"
port = 8787
store = "sqlite:///benchpress.db"
policy_dir = "policies"
auth = "api_key"
console = true

[limits]
requests_per_minute = 600
max_body_bytes = 1048576
idempotency_lease_seconds = 300

[approvals]
ttl_seconds = 3600
webhook_url = "https://hooks.example/benchpress"
webhook_secret = "env:BENCHPRESS_WEBHOOK_SECRET"

[[approvals.rules]]
name = "money-writes"
provider = "stripe"
methods = ["POST", "PUT", "PATCH"]
path = "*"
classes = ["create_charge", "create_invoice"]

[[upstreams]]
provider = "stripe"
base_url = "https://api.stripe.com"
token = "env:STRIPE_SECRET_KEY"
role = "money"
```

`serve --config PATH --host --port --store --policy DIR --auth {api_key,none} --executor module:callable
--no-console` overrides only the flags actually passed; anything else falls through to the file, the environment or
the defaults.

## 9. Store

SQLite (`sqlite:///benchpress.db`, the default) needs nothing else installed; `benchpress-agent[server]` is enough.
For more than one replica, point `store` at Postgres (`postgresql://user:pass@host:5432/db`, normalized internally
to `postgresql+psycopg://...`) and install the `postgres` extra (`pip install "benchpress-agent[server,postgres]"`,
which the container image already includes). `Store.open` migrates to head by default, so `serve` never needs a
separate migration step against a fresh database — but for an ops-controlled rollout:

```
benchpress db upgrade --store postgresql://benchpress:benchpress@localhost:5432/benchpress
benchpress db current --store postgresql://benchpress:benchpress@localhost:5432/benchpress
```

`benchpress workspace create NAME --store ...` and `benchpress workspace key NAME --name KEYNAME --store ...`
provision workspaces and additional API keys outside the bootstrap flow; every key is stored only as its SHA-256
hash. Back up a SQLite store like any other file (the `benchpress-data` Docker volume, or the `.db` file itself);
back up Postgres with your usual tooling (`pg_dump`, a managed snapshot).

## 10. Metrics and tracing

`GET /metrics` (gated by `metrics_auth`) exposes, on its own `CollectorRegistry` per app:

- `benchpress_writes_total{provider, status}` — every finished write, by outcome
- `benchpress_refusals_total{rule}` — refused writes, by the gate rule
- `benchpress_readback_mismatches_total{provider}`
- `benchpress_approvals_total{event}` — `requested` / `approved` / `denied` / `expired`
- `benchpress_write_seconds{provider}` — a histogram of end-to-end write time
- `benchpress_http_requests_total{route, method, code}` and `benchpress_http_request_seconds{route}` — every HTTP
  request, MCP included (labelled `route="/mcp"`)

`provider` is the action's own provider name when it is bundled (a playbook provider) or a configured upstream,
else `"other"` — a caller cannot mint unbounded label series from a free-text `/v1/execute` body.

Tracing spans are emitted per phase and write only when the **host process** configures its own OpenTelemetry SDK;
without one, `benchpress.telemetry.span` is a no-op, and without the `opentelemetry` package installed at all it
never even imports it. The gateway never configures an exporter and never sends anything anywhere on its own —
there is no telemetry by default.

## 11. Security

- API keys are never stored in plaintext — only their SHA-256 hash — and are printed exactly once, at creation.
- `auth = "none"` and `metrics_auth = "none"` are accepted only when `host` is a loopback address; `benchpress ui`
  refuses to bind anywhere else, since it has no authentication at all.
- Secrets in `benchpress.toml` (`approval_webhook_secret`, an upstream `token`) must be `env:NAME` references; a
  literal value is a config error, and no secret is ever logged or written into a receipt.
- Every action's `headers` are rejected (`422`, before the gate runs) if they carry a credential-shaped key
  (`authorization`, `x-api-key`, `api_key`, `token`, `secret`, `password`, `cookie`, case-insensitive) — credentials
  belong in upstream configuration, never in a write's own headers.
- A per-key token-bucket rate limit (`requests_per_minute`, default 600/min) and a request body cap
  (`max_body_bytes`, default 1 MiB) bound a long-lived gateway; a benchmark's per-trial call budgets do not apply.
- No telemetry by default (section 10).
- **Not every route needs a key.** `/healthz` is intentionally public, and FastAPI's own `/docs`, `/redoc` and
  `/openapi.json` are currently served without authentication — do not expose a gateway's port to an untrusted
  network expecting only `/healthz` to be reachable unauthenticated.

## 12. Performance

Measured 2026-09-15 on an Apple M1 (8 cores, 8 GB RAM), macOS 26.6.2 (build 25G83), Python 3.12.13 (uv-managed
CPython), with the load generator running on the **same machine** as the server, in a separate process competing
for CPU — a floor for this laptop, not a capacity claim.

**In-process, 5,000 writes** (`uv run python scripts/loadtest.py --in-process 5000`):

```json
{"max_ms": 39.349, "mean_ms": 0.415, "n": 5000, "p50_ms": 0.321, "p99_ms": 1.626}
```

**Gate: p99 < 10 ms — PASS**, with 8.374 ms of headroom (p99 is 6.2x under the gate).

**Gateway, 500 requests/second offered for 20 s, concurrency 64** — the perf gate's target rate. The per-key rate
limit was raised to 1,000,000/minute for this run only (`[limits] requests_per_minute`), so the limiter itself
never became the bottleneck. Two runs, back to back:

| Run | `achieved_rps` | `errors` | `p50_ms` | `p99_ms` | `service_p50_ms` | `service_p99_ms` |
|---|---|---|---|---|---|---|
| 1 | 116.722 | 6 (3 HTTP 500 `database is locked`, 3 transport) | 13248.106 | 65007.166 | 322.16 | 4299.92 |
| 2 | 203.373 | 0 | 14534.169 | 28986.988 | 202.333 | 1807.662 |

**Gate: 500 rps — MISS.** Achieved throughput was 116.7-203.4 rps, 23-41% of the 500 rps target; the two runs
differ by roughly 2x on this machine.

**Gate: p99 < 25 ms via the gateway — MISS at 500 rps.** Once offered load exceeds capacity, requests queue for
the whole run: `p50_ms`/`p99_ms` are measured from each request's due time in an open loop, so they include that
queueing (2,600x and 1,160x over the 25 ms gate in runs 1 and 2). Service time alone (from when a request was
actually sent) is far lower — 4,300 ms and 1,808 ms p99 — but still well over the gate under this load.

**Below saturation, 100 rps for 20 s** (context, not the gate's rate): `{"achieved_rps": 100.006, "errors": 0,
"p99_ms": 15.664}` — p99 is 9.34 ms under the 25 ms latency gate, with zero errors. The latency gate is only missed
once offered load passes this machine's capacity, which sits somewhere between about 117 and 203 rps here.

Nothing was tuned to hit these numbers: the default rate limit (raised only as noted above), the gate, read-backs
and the store are all unchanged from what a real deployment would run.

## 13. Limits in this pre-release

- **No OIDC or RBAC.** Every key belongs to a workspace, full-access within it. Planned for Sprint 6.
- **No Slack approvals and no per-workspace quotas.** The approval queue here is HTTP + webhook only. Planned for
  Sprint 3.
- **No LangChain adapter yet.** The 100-call parity fixture proves library, HTTP and MCP callers agree; the
  LangChain leg lands with its adapter in Sprint 2.
- **The upstream executor keeps its scratch-safety rails.** `RealAppGateway` (the default `serve --executor`) still
  refuses a non-loopback upstream `base_url` unless `BENCHPRESS_SCRATCH_OK=1` is set, and still accepts only
  `sk_test_` Stripe keys. A misconfigured upstream stops `serve` before it binds a port, with a config error and
  exit code 2 — never a traceback, and never a silent connection to a real account.
