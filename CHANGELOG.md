# Changelog

All releases are on PyPI as [`benchpress-agent`](https://pypi.org/project/benchpress-agent/) and tagged in git.
Every release passed the full gate (pytest, ruff, pyright strict, the task-agnostic grep) and was installed
back from PyPI into a clean environment before it was announced.

## 0.6.0 (2026-09-13)
- **GitHub playbook** (`github`, also registered as the `code_host` role): issue and pull-request search with look-alike
  evidence, policy sources from CONTRIBUTING / SECURITY / CODEOWNERS and policy-labelled issues, issue updates
  (including add-only labels) and comments, each with a read-back. The `release-engineering` policy pack now has a
  real provider behind it. See [docs/PLAYBOOK-GITHUB.md](docs/PLAYBOOK-GITHUB.md).
- Gate: `owner/repo#N` identifiers are matched against the request path, so a look-alike issue is refused even
  without a declared target; `merge-upstream`, `update-branch` and issue close are classified (merge, push,
  close). Corpus: 178 cases, all passing (26 GitHub).
- Offline end-to-end test: the full loop on an in-memory GitHub picks the right issue among look-alikes, the gate
  refuses the planted write, and the change is read back.

## 0.5.0 (2026-09-13)
- **Composio** (optional `[composio]` extra, verified against `composio` 0.21.1):
  `benchpress.shims.composio.guard_composio(Composio(), policy, receipts=...)` checks every tool execution against
  the shared guard policy **before** it reaches Composio (declared classes, policy classes, tool metadata hints,
  then the shared name heuristic), refuses without executing, and writes a JSONL receipt per call. By default it
  replaces the client's own `execute` and the provider execute hook, so the unguarded path is closed.
  `composio_executor(client, user_id=...)` lets the full Benchpress loop drive Composio tools through the gate.
  See [docs/COMPOSIO.md](docs/COMPOSIO.md).
- The name heuristic and refusal message now live in `benchpress.shims.guard_policy`, shared by all three guards.

## 0.4.0 (2026-09-13)
- **OpenAI Agents SDK** (optional `[openai-agents]` extra, verified against `openai-agents` 0.22.2):
  `benchpress.shims.openai_agents.guard_tools(tools, policy, receipts=...)` returns guarded copies of your
  `FunctionTool`s. Every call is classified (read / write / destructive) and checked against the policy **before
  the tool body runs**, under `Runner.run`, `run_sync`, streaming and agents-as-tools; refusals go back to the model
  as a tool error string and every call leaves a JSONL receipt. Policy packs apply to tools mapped to provider
  requests. See [docs/OPENAI-AGENTS.md](docs/OPENAI-AGENTS.md).
- One policy format for both shims: the guard policy model moved to `benchpress.shims.guard_policy`, shared by
  `mcp-guard` and the OpenAI Agents guard.

## 0.3.3 (2026-09-13)
- **Last corpus gap closed: fewer false refusals.** A bare filename in a message (`summary.pdf`, `index.html`) is
  no longer mistaken for an external domain; real external domains, external URLs and external email addresses
  are still refused (new cases prove both directions; `report.zip` stays refused because `.zip` is a real TLD).
  Corpus: 152 case(s): 152 passed, 0 xfail, 0 failed, 0 xpass. Every known gate gap found by the public corpus is now fixed.

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
