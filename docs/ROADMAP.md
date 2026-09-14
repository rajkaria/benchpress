# Roadmap

Benchpress is becoming the open-source execution layer for AI agents that act on real systems: one library for the individual developer, one container for the team, one Helm chart for the enterprise. Same code, same policy format, same receipt format, Apache-2.0 throughout.

Status is tracked here in public. Each sprint ends in a tagged pre-release (`1.0.0aN`), a CHANGELOG entry and a regenerated site. Dates are targets, not promises.

## Sprint 0: Foundation (week 1)

Goal: make the repository safe to promote and ship the two primitives every later phase composes on.

- [x] Python 3.11 floor across the library, with CI running the test suite on 3.11, 3.12 and 3.13
- [x] ArgaBench harness made fully optional: tests skip cleanly without it, and CI only vendors it behind an explicit flag
- [x] Receipt schema v1, versioned and shipped as package data
- [x] `ToolSpec`, the shared tool metadata contract, classified from spec first and a name heuristic second
- [x] `VerifiedWrite`, the gate-to-execute-to-read-back-to-evidence primitive, usable without the full controller
- [ ] Contributing guide, security policy, code of conduct, issue and PR templates, and this roadmap
- [ ] README rewritten around three doors, PyPI readme, CHANGELOG entry, `v1.0.0a1` tag

## Sprint 1: Gateway + local console (weeks 2-3)

Goal: `benchpress serve` and `docker run` both exist, and a LangChain agent and an MCP client going through the same gateway produce identical receipt lines.

- [ ] Receipts, writes, approvals, policies and workspaces stored durably, on SQLite or Postgres
- [ ] Gateway HTTP API: execute a write, list receipts, manage approvals and policies, health and metrics endpoints
- [ ] Approval queue with a time-to-live and webhook notifications; a parked write resumes with the same fingerprint
- [ ] Remote MCP server surface alongside the existing local proxy
- [ ] `benchpress serve` command with a config file, per-workspace API keys and rate limits
- [ ] Tracing spans and metrics counters per phase and write
- [ ] Local console: a receipts list and a receipt detail view, served by the gateway or standalone
- [ ] Container image published to a public registry; `docker run` serves the console and the API together
- [ ] Parity fixture proving library mode and gateway mode produce byte-identical receipt lines for the same calls

## Sprint 2: Adapters wave 1 (weeks 3-4)

Goal: one line of integration in any mainstream agent framework routes tool calls through Benchpress, in library or gateway mode.

- [ ] LangChain / Deep Agents middleware
- [ ] Claude Agent SDK (Python) hooks
- [ ] Pydantic AI guard
- [ ] CrewAI tool wrapper
- [ ] Google ADK callback
- [ ] Gateway mode added to the existing OpenAI Agents, Composio and MCP adapters
- [ ] Arcade tool wrapper
- [ ] TypeScript SDK: Vercel AI SDK guard
- [ ] TypeScript adapters for the Claude Agent SDK, OpenAI Agents JS, LangChain JS and Mastra
- [ ] Claude Code plugin: hooks and a slash-command skill for gating tool calls
- [ ] Plugin for OpenClaw-style tool middleware, with an approval surface
- [ ] GitHub Action that runs gate checks and regression on uploaded receipts
- [ ] A runnable example and a docs page for every adapter, each passing the same 100-call parity fixture

## Sprint 3: Console + policy (weeks 4-5)

Goal: approvals, policy simulation and cost controls run inside the console, with Slack and external policy engines wired in.

- [ ] Approval inbox in the console, plus Slack approvals with interactive buttons
- [ ] Policy editor and simulator: see which past writes would flip under a proposed policy change
- [ ] Gate corpus and drift screen, a rehearsal screen, and cost and budget limits per workspace and provider
- [ ] Bridge to external policy engines (OPA and Cedar) so their rules evaluate inside the gate
- [ ] Browser-driven smoke-test suite for the console, with a performance score gate in CI

## Sprint 4: Providers, record/replay, twins (weeks 5-6)

Goal: new provider playbooks are fast to add and safe to test without live credentials.

- [ ] Playbook generator that starts from an OpenAPI spec, with a conformance test suite
- [ ] Ten new playbooks: Salesforce, Linear, Jira, Notion, Google Drive, Google Calendar, Zendesk, Intercom, Workday and Postgres
- [ ] Record and replay: capture a run as a redacted stage, then rehearse it offline with no network calls
- [ ] Local provider twins (Slack, HubSpot, Stripe, Gmail) seeded with synthetic data and reset over an API
- [ ] Contributor docs and "good first playbook" issues for each new provider

## Sprint 5: Evals and the scoreboard (weeks 5-7)

Goal: benchmark Benchpress against public agent-safety suites and publish the numbers, whatever they say.

- [ ] Evals runner comparing an unguarded agent, a Benchpress-guarded agent, and ablations, on pass rate, unsafe-write rate, over-refusal, cost and latency
- [ ] tau2-bench driver (retail, airline, telecom)
- [ ] AgentDojo driver, a TheAgentCompany subset, and an ArgaBench driver for anyone with API access to it
- [ ] Published reports, site tiles and a public leaderboard page that takes submissions by pull request
- [ ] `benchpress eval` command so anyone can run the same suite against their own agent

Decision gate at the end of Sprint 5, stated here so it stays honest: **publish whatever the numbers say.** If Benchpress shows no measurable lift on two independent graders, the launch narrows from a benchmark-win claim to "a gate, a verifier and a receipt for any agent" — still shipped, just described differently.

## Sprint 6: Enterprise (weeks 7-8)

Goal: the pieces a team needs to run Benchpress with single sign-on, audit and export.

- [ ] Helm chart with optional highly-available Postgres and autoscaling, installable on a fresh cluster in under 15 minutes
- [ ] OIDC single sign-on, role-based access control (viewer, approver, policy admin, owner), and workspaces
- [ ] Hash-chained receipts with optional signing, and an audit-verify command that detects tampering
- [ ] Exports to SIEM tooling, object storage and OpenTelemetry, with retention and redaction configurable per workspace
- [ ] Secret references (environment variables, Vault, cloud KMS), an air-gapped install guide, and on-prem model endpoints
- [ ] Compliance mapping docs: SOC 2, EU AI Act Article 12 and OWASP Agentic, control by control against receipt fields

## Sprint 7: Docs, release engineering, launch (weeks 8-9)

Goal: a real docs site, a working example for every framework, and a signed, reproducible release.

- [ ] Documentation site covering every adapter, provider and CLI command
- [ ] One runnable example per framework, tested offline in CI
- [ ] Browser-based trace-replay demo: paste a trace, see what the gate would have refused, share the link
- [ ] Release engineering: trusted publishing to package registries, signed container images, a software bill of materials, dependency and secret scanning
- [ ] Upstream contributions: framework integration PRs, plugin marketplace listings, registry and template submissions
- [ ] Public launch day: blog post, community threads, a public benchmark submission

## Sprint 8: Design partners (weeks 9-12)

Goal: real teams run the gateway against production CRM, billing and code systems, and every incident becomes a public test case.

- [ ] Design partners running the gateway against a production system
- [ ] Each incident converted into a public, regression-tested gate-corpus case
- [ ] Weekly releases through the quarter
- [ ] Next-minor-version scope set from what design partners actually hit

## Decisions

- Apache-2.0 for everything, including the enterprise features, with no open-core split — adoption comes first, and a license split kills enterprise trials before they start.
- The name stays Benchpress: PyPI package `benchpress-agent`, npm package `benchpress` (falling back to `@benchpress/sdk`), with `benchpress-guard` kept as a re-export — a rename would cost more than it's worth.
- No monorepo rewrite. The gateway ships inside the main package as an optional extra, the console builds to static assets served by the package, and the TypeScript SDK stays a separate package — this keeps the existing test suite and every import path intact.
- Python 3.11 is the floor, with strict type checking throughout — the 3.12-only syntax in use was a handful of lines, and 3.11 roughly doubles the installable base.
- SQLite by default, Postgres for teams and enterprise, one ORM and one migration tool for both, nothing else in the 1.0 line — keep the storage surface area small.
- FastAPI and uvicorn power the gateway, and the official MCP SDK powers the MCP surface — both are already dependencies, so this is the smallest addition that works.
- The console is built with React, Vite and TypeScript on top of unstyled primitives, shipped as static assets — one build artifact, served by the gateway or standalone.
- No telemetry by default. An opt-in, anonymous daily ping (version, OS, adapter names) is available and documented — trust matters more than usage data.
- One version number spans the Python package, the npm package, the container image and the Helm chart, with pre-releases tagged `1.0.0aN` — one release train, no version drift between artifacts.
- Contributions use the Developer Certificate of Origin, not a contributor license agreement. Breaking changes go through an RFC first, and the maintainer makes the final call — standard practice for a solo-maintainer open-source project.
- The ArgaBench harness is never shipped or vendored into a release; CI only pulls it in when explicitly enabled, and every test that depends on it skips cleanly otherwise — legal hygiene ahead of wider promotion.

## How to follow along / contribute

Every sprint's progress shows up here as checkboxes, plus a CHANGELOG entry and a tagged pre-release. To contribute code, a playbook, a policy pack or a gate-corpus case, start with [CONTRIBUTING.md](../CONTRIBUTING.md). To report a bug, request a feature, or propose a playbook, open an issue on the [issue tracker](https://github.com/rajkaria/benchpress/issues).
