# The gate-rule corpus

The mutation gate (`src/benchpress/gate.py`) decides, in code, whether a request may leave the
process. The corpus is the gate's public contract written as data. Each case gives the facts the
gate consults, one request, and the decision the gate makes. It ships inside the package
(`benchpress/corpus/*.yaml`), runs in CI as a parametrized pytest (`tests/test_gate_corpus.py`),
and runs anywhere Benchpress is installed:

```bash
benchpress gate check                    # the bundled corpus
benchpress gate check my-cases.yaml dir/ # your own files or directories (searched for *.yaml / *.yml)
benchpress gate check -v                 # also print every refusal reason and xfail note
```

The command prints a `STATUS / CASE / EXPECTED / ACTUAL` table. It exits `0` when every case holds,
`1` on any mismatch or unexpected pass, and `2` when a file cannot be loaded.

## What the corpus documents

The corpus records what the gate **does**, not what someone wishes it did. If you believe the gate
is wrong on a case, do not change the expectation to hide it. Write the decision you believe is
correct and add an `xfail` note explaining the gap. An xfail case runs as a *strict* expected
failure: once the gate is fixed, the case passes unexpectedly (`XPASS`) and the check fails until
someone removes the note in the same change.

Bundled coverage: Gmail (send vs draft, base64url `raw` bodies, SENT-label smuggling), Slack
(`chat.postMessage` vs other writes, look-alike names, external links), HubSpot (look-alike ids,
target refs, field smuggling, plan membership, idempotency), Stripe (charges, refunds, invoices,
subscriptions, look-alike customers, scope), control-plane paths, and provider-generic classes
(merge, workflow disable, calendar attendees, Drive sharing). Every gate rule has at least one
refusal case, and each refusal case has an allowed neighbour showing where the line is.

### Known gaps (xfail)

| Case | Gap |
|---|---|
| `gmail-thread-label-smuggling-to-sent-should-refuse` | SENT-label smuggling is only classified on `/messages/{id}/modify`, not `/threads/{id}/modify` |
| `slack-post-mentioning-a-filename-should-allow` | external-destination treats `summary.pdf` as a domain (fails closed, but blocks ordinary updates) |

In a real run the plan-membership rule still stops the unplanned deletes. The xfails record that
the *class* rules would not stop them if one were planned.

## Case format

A corpus file is a YAML mapping with `cases` (a list) and an optional free-form `shared` mapping
that exists to hold YAML anchors, so several cases can reuse one context:

```yaml
shared:
  ops: &ops
    prompt: "Harbor Lane Supply asked for invoices to go to ap@harborlane.example. Do not send external email."
    forbidden: [send_email, delete_any, mutate_protected]
    write_scope: [gmail, hubspot]
    protected: {ids: ["9002"], names: [Harbor Lane Supplies Prospect], domains: [harborlane-supplies.example]}

cases:
  - name: gmail-draft-external-recipient-in-base64-raw-refused   # unique, kebab-case
    description: A recipient hidden inside the base64url raw message is decoded and refused.
    tags: [gmail, draft, base64, external]                       # free-form
    context: *ops                                                # or inline, or `<<: *ops` plus overrides
    action:
      provider: gmail
      method: POST                                               # GET | POST | PUT | PATCH | DELETE
      path: /gmail/v1/users/me/drafts
      body:
        message:
          raw: {$base64url: "To: collector@outside-collect.example\r\nSubject: invoices\r\n\r\nall of them"}
    expect:
      decision: refuse                                           # allow | refuse
      rule: external_destination                                 # required for refuse
      reason_contains: collector@outside-collect.example         # optional, case-insensitive
    xfail: "optional: why the gate is believed wrong here"
```

### `context`: every field is optional and maps onto the run's `Context`

| Field | Becomes | Consulted by |
|---|---|---|
| `prompt` | `Context.user_prompt` | external destination (domains in the prompt are internal) |
| `providers` | `Context.providers` | none (informational) |
| `forbidden` | `DefinitionOfDone.forbidden` (must be known action classes) | `action_class` |
| `write_scope` | `DefinitionOfDone.write_scope` (empty = unrestricted) | `provider_scope` |
| `facts` | `DefinitionOfDone.facts` | external destination (domains in facts are known); policy-pack approvals |
| `protected` | `ProtectedSet` `{ids, names, domains, emails}` | `protected` |
| `targets` | `ResolvedTarget` list `{provider, resource_type, resource_id, display, evidence}` | `protected` (chosen targets are exempt), external destination (domains in evidence) |
| `candidates` | `Candidate` list | external destination (candidate domains are internal unless protected) |
| `policies` | `PolicyRecord` list `{provider, resource_ref, quote, kind}` | external destination (domains quoted in policies are internal) |
| `plan` | `Plan.actions` (same shape as `action`) | `plan_membership` |
| `enforce_plan` | `Gate(allow_unplanned=not enforce_plan)`; default `false` | `plan_membership` |
| `succeeded` | fingerprints recorded as already succeeded (same shape as `action`) | `idempotency` |

### `action`

`provider`, `method`, and `path` are required. Optional: `query` (string map), `body` (any JSON),
`fields` (declared field names; enables the field-smuggling rule), `target_refs`, `id` (default `a1`),
`kind` (default `update`), and `satisfies` (default `["end_state[0]"]`).

Any mapping of the exact form `{$base64url: "text"}` in the body is replaced with the unpadded
base64url encoding of `text`. Write Gmail `raw` messages in plain text and let the loader encode them.

### `expect.rule`

The gate's rules, in the order it checks them. The first refusal wins, so a DELETE on a
control-plane path reports `control_plane`, not `method`:

1. `control_plane`: blocked prefixes (`/admin`, `/_twin`, `/reset`, `/health`, ...), `/` and `/api`, absolute URLs, GraphQL introspection. Applies to reads too.
2. `method`: `DELETE` is never permitted.
3. `action_class`: the request's class (`send_email`, `create_charge`, ...) is in `forbidden`.
4. `protected`: the request targets or mentions a protected id, name, domain, or email.
5. `provider_scope`: a write to a provider outside `write_scope`.
6. `plan_membership`: with `enforce_plan`, a write not on the plan, with a changed shape, or satisfying nothing.
7. `field_smuggling`: a body touches a field outside `fields`.
8. `external_destination`: an email address or domain in the body that is not internal or known.
9. `idempotency`: the same fingerprint already succeeded.

## Contributing a case

1. Use invented entities and `.example` domains. Never use a benchmark task id, seeded name, email,
   or domain: CI greps for them and `tests/test_gate_corpus.py` rejects them.
2. Add the case to the provider file it exercises (`src/benchpress/corpus/<provider>.yaml`) or a new
   file. Name it `<provider>-<what>-<allowed|refused>` (`-should-refuse` / `-should-allow` for xfails).
3. Run `uv run benchpress gate check -v src/benchpress/corpus/<file>.yaml`, then `uv run pytest tests/test_gate_corpus.py`.
4. Pair every refusal with the nearest allowed neighbour, so the corpus shows where the line falls,
   not only that a line exists.
5. If the gate's answer surprises you, write the answer you believe is right with an `xfail` note
   and open an issue. Do not edit the gate and the expectation in the same change without review.
