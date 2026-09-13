# Changelog

All releases are on PyPI as [`benchpress-agent`](https://pypi.org/project/benchpress-agent/) and tagged in git.
Every release passed the full gate (pytest, ruff, pyright strict, the task-agnostic grep) and was installed
back from PyPI into a clean environment before it was announced.

## 0.3.2 (2026-09-13)
- **Gate gaps found by the public corpus, closed.** Deletes spelled as write routes (`chat.delete`,
  `batchDelete`, `batch/archive`, `:delete`, GraphQL deletes) are now classified as deletes, and marking mail
  SENT through `threads/{id}/modify` or `messages/batchModify` is classified as sending email. Corpus: 147 case(s): 146 passed, 1 xfail, 0 failed, 0 xpass.
  These make the gate stricter; trials graded before this release ran on the earlier gate.

## 0.3.1 (2026-09-13)
- `benchpress run --policy-pack NAME` (repeatable) enforces policy packs in the gate. Unknown or malformed
  packs are a usage error before any provider call.

## 0.3.0 (2026-09-13)
- **Policy packs** (`billing`, `customer-success`, `it-offboarding`, `release-engineering`): YAML rule sets the
  gate enforces in code, with the rule id named in every refusal. Packs only ever add refusals; with no pack
  the gate behaves exactly as before. `benchpress policy list|show`, `wrap(..., policy_packs=...)`,
  `run_trial(..., policy_packs=...)`. See [docs/POLICY-PACKS.md](docs/POLICY-PACKS.md).
- **Public gate-rule corpus**: 139 YAML cases bundled in the package, `benchpress gate check [PATH ...]`.
  134 pass; 5 known gate gaps are recorded as strict xfails rather than hidden. See
  [docs/GATE-CORPUS.md](docs/GATE-CORPUS.md).
- **Rehearse / replay** (`benchpress.rehearse`): run a request N times on fresh stages; converged only with an
  identical normalized final-state hash, identical writes and zero refusals. `replay` executes a converged plan
  with no model, through the gate, substituting generated ids and reading back every write.
  `benchpress rehearse` / `benchpress replay`. See [docs/REHEARSE.md](docs/REHEARSE.md).
- Runtime dependency added: `pyyaml`.

## 0.2.0 (2026-09-13)
- **MCP** (optional `[mcp]` extra): `benchpress.shims.mcp.mcp_executor` drives MCP tools through the tool bus
  and gate; `benchpress mcp-guard --policy guard.json -- <upstream>` is a stdio MCP proxy that refuses writes
  unless a policy rule allows them and writes a JSONL receipt for every call. See [docs/MCP.md](docs/MCP.md).

## 0.1.1 (2026-09-13)
- `benchpress demo`: the whole loop offline on an in-memory workspace with a scripted model. No keys, no
  network; watch the gate refuse a planted write to a look-alike record and get `receipt.json` + `receipt.html`.

## 0.1.0 (2026-09-13)
- First public release. `benchpress.wrap(model, executor, providers=[...])` puts the eight-phase loop around
  any `execute_tool`-shaped tool layer; `Benchpress.run` / `run_sync`. Real-app gateway for Slack, Gmail,
  HubSpot and Stripe; `benchpress run` and `benchpress receipt --html`. Apache-2.0, typed (`py.typed`).
