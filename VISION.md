# Benchpress: product vision

## What we built today

A task-agnostic control loop that makes a frontier model finish multi-app work safely: policy
sweep, locked look-alikes, a code-enforced mutation gate, read-back verification and reviewed
deliverables. It runs on real Slack, Stripe, HubSpot and Gmail, and it is measured with Arga Labs'
own benchmark grader.

## The problem it solves

Agents got write access to billing systems, CRMs and repos in 2026. Arga's data shows the best
configurations still fail about 30% of realistic ops tasks, and 17% of all runs do something
unsafe. The failures come from the scaffold: agents don't read policy, can't tell look-alike
records apart, and report success from a 2xx. Every team shipping an agent with write access
rebuilds guardrails by hand, in prompts, and learns about the failures from customers.

## What this becomes

### Month 1: open core
- Publish `benchpress-agent` on PyPI: `wrap(model, executor)` over any `execute_tool`-shaped tool layer. Shims for MCP, Composio, OpenAI Agents SDK, Vercel AI SDK.
- Upstream the ArgaBench adapter as a community profile and publish a full 40-task row on Arga twins.
- Ship playbooks for the 12 twin providers, plus a public gate rule corpus that anyone can add cases to.
- **First 10 design partners:** Arga's customers (Respan, Aemon, Bolto, Rho, Monaco, Weave, Slash) and the teams building CS/RevOps agents (the Userlens category).

### Month 3: Rehearse
- Production mode. When a request arrives, fork the relevant live state into Arga twins, run the plan N times, and require a converged final-state hash with zero refusals. Then replay the converged, typed plan against production with read-back on every write.
- **The model never decides a production write live.** It decides in rehearsal, and code replays the result.
- Hosted receipts with audit export (SOC 2 evidence for "who changed this customer record and why").

### Month 6: closed loop
- Lemma-fed regressions: every silent failure Lemma groups in production becomes a twin scenario and a permanent Benchpress regression test.
- Policy packs per function (billing, CS, IT offboarding, release engineering), authored in YAML and enforced by the gate.
- Target: 1,000 verified tasks/day across 30 teams. Unsafe rate 0 by construction on gated classes.

## Why each integration deepens over time

| Partner | Today | Month 3 | Month 6 |
|---|---|---|---|
| **Arga Labs** | Benchmark scenarios and grader are the proof | Twins become a *runtime* rehearsal stage, not only CI. That opens a second market for Arga | Every Benchpress customer is an Arga twin-run customer. Scenarios generated from real incidents |
| **Lemma** | Optional span tracing per phase and tool call | Silent-failure issues open as Benchpress regression scenarios | Online evals gate plan changes before rollout |
| **Userlens-style CS agents** | The renewal-rescue and billing-contact workflows | Policy packs for customer outreach: draft, owner review, send by human | Verified-task receipts in the account timeline |

## Revenue model

- **Per verified task** for teams: $0.50–$2 per task that completes with full evidence. Refused and escalated tasks are free, which aligns our incentive with safety.
- **Platform tier** ($2k–$10k/mo) for rehearsal volume, hosted receipts, audit export and policy packs.
- Unit economics at 1,000 tasks/day × $1 is about $30k MRR on ~$4–6k of model spend.

## What the hackathon validated / what is still a hypothesis

- **Validated:** scaffold structure, not model choice, moves pass and unsafe rates on the hardest published tasks, and the lift is measurable with a third-party grader (see `docs/RELIABILITY-BRIEF.md` for exact numbers and limits).
- **Hypothesis:** teams will pay per verified task rather than build guardrails in-house. The rehearsal convergence gate generalizes beyond seeded scenarios.

## The ask

- Arga: multi-twin access to run and publish the full 40-task row on twins, and an intro to two customers for a paid pilot.
- Lemma: a design-partner slot for the silent-failure → scenario loop.
