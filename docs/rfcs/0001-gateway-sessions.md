# RFC 0001: Gateway sessions, context ownership and budgets

Status: Accepted (2026-09-14)

## Summary

The Benchpress gateway puts `VerifiedWrite` behind HTTP for many callers and many replicas. A stateless HTTP call
cannot supply three things the library gets from its caller or its loop, so this RFC fixes them. The context the gate
judges a write against belongs to the caller and is stored as a session. A replayed write is recognised across
replicas by claims in the shared store, scoped per session or per workspace. A long-lived gateway is bounded by
per-key rate limits and a body cap, not by the benchmark's per-trial call budgets.

## Motivation

The gate needs a context, and a stateless HTTP call has none. The gate's rules read:

- the user's request (`external_destination` allows only domains named there or in the targets)
- the protected set
- the resolved targets
- the authorised write scope
- the forbidden action classes

Inside the benchmark loop, the phases before execution discover all of that. A bare `POST` carries none of it, and
the gateway has no loop to discover it. Take "send renewal notices for Rivermill Studio to ap@rivermill.example":
updating company 701 is right or wrong depending on whether 701 is the customer the user meant and whether 702 is a
look-alike that must not be touched. Only the caller knows which.

State is also needed for replays. Without claims that outlive one request and one process, a retry that lands on a
second replica behind a load balancer would write a second time.

## Design

### Context ownership

The caller supplies the context; the gateway never infers any of it. `ContextInput` carries `user_prompt`,
`providers`, the `protected` set (`ids`, `names`, `domains`, `emails`), `targets`, `write_scope` and `forbidden`.
`ContextInput.to_context(session_id)` builds the library's `Context`: protected domains are casefolded, protected
emails are canonicalised, and `write_scope` and `forbidden` become `DefinitionOfDone(write_scope=..., forbidden=...)`.

### Sessions are rows

`POST /v1/sessions` persists the session (workspace, id, context, idempotency scope) before it answers 201. A taken
id is 409; an omitted id is generated as `ses_` plus 16 hex digits. Any replica can rebuild a session's writer from
its row. Each replica caches built sessions, least recently used evicted first, keyed by `(workspace_id, session_id)`
and sized by `sessions_cache`. The cache holds nothing safety depends on, because claims live in the store, and a
cached writer keeps no per-write history (`history_limit=0`), so the cache does not grow with traffic.

`POST /v1/execute` takes exactly one of `session_id` or an inline `context`. An inline context creates a session
first, and the response names it.

### Idempotency

Every session's `VerifiedWrite` shares one `SqlIdempotencyStore` over the gateway's store, with the lease from
`idempotency_lease_seconds`. The claim scope is:

- `"{workspace_id}:{session_id}"` for a session created with `idempotency_scope = "session"` (the default)
- `workspace_id` for a session created with `idempotency_scope = "workspace"`, so all such sessions share claims

A byte-identical write already done in the scope is refused with rule `idempotency`. An identical write still in
flight on any replica is refused too, not queued behind the first (in-flight refusal). A failed write releases its
claim so a retry is sent, and a claim older than the lease is taken over, so a crashed replica never blocks a write
for good.

### Budgets

Writes run on `VerifiedWrite`'s unbounded tool bus: no per-phase budgets, no delivery reserve and no lifetime call
cap. The default executor, `RealAppGateway`, is built with both of its lifetime call caps lifted and keeps no trace;
the store's receipts are the record. The gateway's limits are:

- a per-API-key token bucket (`requests_per_minute`); a request over it is 429 with `Retry-After: 1`
- the request body cap (`max_body_bytes`); a larger body is 413, checked from `Content-Length` and enforced while
  the body is read

Per-workspace write quotas are planned for Sprint 3.

### Gate strictness and policy packs

Sessions build `VerifiedWrite(..., allow_unplanned=True)`: the gateway has no plan, and plan membership is a loop
concept. Every other gate rule applies unchanged.

A session's policy packs are every `*.yaml` pack in `policy_dir`, followed by the workspace's stored packs. The
directory is loaded once at startup, and a bad pack stops startup with a configuration error. Stored packs come from
`PUT /v1/policies/{name}` (raw YAML, whose `name` must equal the path) and are parsed when a session is built.
`PUT` and `DELETE` drop the receiving replica's cached sessions for the workspace, so its next request rebuilds them
with the current packs. Other replicas apply the change to every session they build afterwards; a session another
replica already has cached keeps the packs it was built with until that replica evicts it.

### Receipts

`execute` appends exactly one `benchpress-write/1` line per call, refusals included. It is written by the same
`write_line` the library uses, with `at` from the gateway's clock, `workspace` set to the workspace name and `session`
to the session id, and its row id is returned as `receipt_id`. `GET /v1/receipts` filters by customer, provider,
rule, status, session and event, newest first, paging with a `before` cursor.

### Rejections

Before any gate evaluation or provider call, `execute` answers 422 for:

- an action whose `headers` carry a credential-like key (`authorization`, `x-api-key`, `api_key`, `token`, `secret`,
  `password` or `cookie`, in any case), because credentials belong in the upstream configuration
- an action whose method is `GET`, because reads go through `read` and only writes are executed
- a request with both or neither of `session_id` and `context`

`read` is not gated, but a path the tool bus statically blocks (a control-plane root or prefix) is 422 before any
provider call.

### Isolation and auth

Every lookup is scoped to the caller's workspace. A session or receipt id from another workspace is 404, never 403,
so ids do not leak. With `auth = "api_key"` (the default), every route except `/healthz` needs
`Authorization: Bearer bp_…`, and a missing or unknown key is 401 with `WWW-Authenticate: Bearer`. With
`auth = "none"`, which is allowed only on a loopback host, every request acts as the workspace `default`, created on
first use, and no key is rate-limited.

### Approvals

Approvals follow the approval queue's rules, specified with it. A write the gate allows can be parked for a person
when it matches an approval rule. Parking sends nothing and takes no claim, and an approved write passes the gate and
the idempotency claim again when it resumes. This RFC only reserves `needs_approval`, `denied` and `expired` in the
execute status vocabulary.

## Compatibility

Additive; the library is unchanged. The gateway is a new optional surface (`pip install "benchpress-agent[server]"`),
and `import benchpress` loads none of its dependencies. The library only gains:

- `VerifiedWrite.evaluate(action)`, which judges without executing
- `benchpress.packs.policy_pack_from_yaml`, which `load_policy_pack` now delegates to
- `benchpress.tools.interpret_result`, the tool bus's response reader made public
- `RealAppGateway(trace_limit=...)`, whose default keeps the full trace
- `VerifiedWrite(history_limit=...)`, whose default keeps every per-write record
- `benchpress.tools.statically_blocked`, the tool bus's control-plane path check made public

Gateway receipts are `benchpress-write/1` lines; `benchpress-receipt/1` is untouched.

## Safety

- Every write still passes the same `Gate`, with the same rules in the same order, before any provider call, and the
  same read-back decides its status. With the same context, a write gets the same verdict from the library and from
  the gateway.
- The gateway can only add refusals: policy packs from the directory and the workspace, approval parking, and the
  credential-header rejection. The one rule it does not apply, plan membership, has no meaning without a plan.
- The gateway never infers or widens a context. What is protected is exactly what the caller stored.
- Idempotency is stricter across replicas, never looser. Claims live in the shared store, and an in-flight twin is
  refused rather than queued.
- A stored policy change reaches a session another replica has already cached only when that replica rebuilds the
  session. Until then, that session enforces the packs it was built with.

## Alternatives

- **Server-inferred context.** Rejected: the server cannot know what is protected. Guessing look-alikes from provider
  data would make a refusal depend on whatever the gateway happened to read.
- **A queue for in-flight twins.** Rejected: waiting for the first write to finish means polling across replicas, and
  it hides a caller bug (the same write sent twice at once) behind latency. The twin is refused, and the first write's
  receipt says what happened.
- **A per-request context without sessions.** Kept, as the inline `context` on `execute`. It creates a one-shot
  session with the default `session` scope, so it guards nothing across calls. A caller who needs replay protection
  across calls creates a session first (optionally with `idempotency_scope = "workspace"`) and sends its
  `session_id`.
