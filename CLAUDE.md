# Benchpress: project instructions

Hackathon entry for the **Multi-App AI Agent Hackathon, Sunday 2026-09-13, build 09:30–16:00 PT
(22:00–04:30 IST)**. Judges: founders of **Arga Labs** (Phillip Li, Akira Tong) and **Userlens**
(Ankur Dahama, Hai Ta). $10k first prize. Benchpress is the reliability layer for agents with
write access: a task-agnostic control loop, measured against ArgaBench's hardest published scenario.

## Read first, in this order
1. [`docs/SPRINT-PLAN.md`](./docs/SPRINT-PLAN.md): the master schedule (PT/IST), branch decision, blocks, cut lines.
2. [`docs/IMPLEMENTATION-PLAN.md`](./docs/IMPLEMENTATION-PLAN.md): every task with files, interfaces, tests, acceptance commands, subagent prompts.
3. [`docs/STRATEGY.md`](./docs/STRATEGY.md): why it wins, the judge map, the proof ladder, **claim discipline**.
4. [`docs/PLAN-B.md`](./docs/PLAN-B.md): the no-access substrate (real-app rehearsal eval) + verified harness facts.
   [`docs/TRACK-DEVSIM.md`](./docs/TRACK-DEVSIM.md): parallel upside track, grader-faithful local ECOM-02 twins built by multiple agents, 14:00 PT gate.
5. [`docs/BUILD-SPEC.md`](./docs/BUILD-SPEC.md): the full product spec (phases, gate, playbooks, model layer).
6. [`docs/RESEARCH.md`](./docs/RESEARCH.md): verified facts. Do not re-research.
7. Ship docs: [`docs/DEMO-SCRIPT.md`](./docs/DEMO-SCRIPT.md), [`docs/SUBMISSION.md`](./docs/SUBMISSION.md), [`docs/RELIABILITY-BRIEF.template.md`](./docs/RELIABILITY-BRIEF.template.md), [`VISION.md`](./VISION.md).

## Layout
- `arga-twins-benchmark` is a symlink to the vendored clone of ArgaLabs/arga-twins-benchmark at `4a81785` (gitignored). **Never edit graders, gateway, snapshot capture or seeds.**
- `src/benchpress/` is the package. Python 3.12, pyright strict, ruff line 120. Existing: `normalize.py context.py gate.py tools.py` (61 tests).
- `evals/` (Plan B) holds the rehearsal eval: published ECOM-02 seed → real Slack/Gmail/HubSpot/Stripe test → stock-baseline vs Benchpress → ported ArgaBench assertions → reset. The only place scenario data lives.
- `tests/` covers gate, DoD rules, playbooks, replay, real-app gateway, assertion units.
- `reports/` holds committed semantic reports and compare tables. `runs/` is gitignored.

## Hard rules
- **Task-agnostic.** No code keyed on task IDs, seeded names, emails or domains. CI greps for them.
- **Same rules as every candidate.** Two tools, 160/40 calls, 1,800 s, harness system prompt, no control-plane paths.
- **Code beats prompt for safety.** The gate refuses; the prompt only explains.
- **State is truth.** Every write is read back. Status comes from evidence only.
- **Claim discipline** (STRATEGY §8). In Plan B, never say "passed ArgaBench". Say "graded by ArgaBench's verifier on the published seed rebuilt locally".
- **Disclose everything** in the brief: substrate, prep work done 2026-09-12, failures, cost.

## Gates
```bash
uv sync --group dev && uv run pytest -q && uv run ruff check . && uv run pyright
cd arga-twins-benchmark && uv sync --group dev && uv run pytest -q -x
```

## Env
`.env` (copy `.env.example`): `ANTHROPIC_API_KEY` is required. Plan A adds `ARGA_API_KEY`. Real-app mode needs `SLACK_BOT_TOKEN`, `STRIPE_SECRET_KEY` (test), `HUBSPOT_PRIVATE_APP_TOKEN`, optional `GMAIL_ACCESS_TOKEN`.

## Context docs (loaded by the router when you touch matching paths)

| Doc | Covers |
|---|---|
| [`docs/context/agent-core.md`](./docs/context/agent-core.md) | `src/benchpress/**` — model layer, controller, phases, gate, playbooks, real-app gateway, adapter, receipts |
| [`docs/context/evals.md`](./docs/context/evals.md) | `evals/**`, `scripts/**`, `reports/**` — seeders, scenarios, harness bridge, assertions, run loop, compare, gate replay |
| [`docs/context/devsim.md`](./docs/context/devsim.md) | `devsim/**`, `tests/devsim/**` — local twins under the unmodified ArgaBench runner |
| [`docs/context/submission.md`](./docs/context/submission.md) | `README.md`, `VISION.md`, `docs/**` — story, brief, video, form |

## State
Day 2 (2026-09-14) ended at tag `v0.1.0-hackathon`: Benchpress 3/3 pass vs stock 0/3 under the unmodified
ArgaBench grader on twins; real apps 0/2 vs 0/2 (A2 substrate limit). **Start with [`docs/HANDOFF.md`](./docs/HANDOFF.md)**
and [`reports/INDEX.md`](./reports/INDEX.md). Ablations (2026-09-14 14:20 PT): policy sweep off → 0/3, gate off 3/3, read-back off 3/3. Brief PDF at `docs/RELIABILITY-BRIEF.pdf`. Left: record the video (`docs/DEMO-SCRIPT.md`), form, confirmation screenshot.
