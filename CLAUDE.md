# Benchpress: project instructions

Hackathon entry for the **Multi-App AI Agent Hackathon, Sunday 2026-09-13, build 09:30–16:00 PT
(22:00–04:30 IST)**. Judges: founders of **Arga Labs** (Phillip Li, Akira Tong) and **Userlens**
(Ankur Dahama, Hai Ta). $10k first prize. Benchpress is the reliability layer for agents with
write access: a task-agnostic control loop, measured with ArgaBench's own grader.

## Read first, in this order
1. [`docs/SPRINT-PLAN.md`](./docs/SPRINT-PLAN.md): the master schedule (PT/IST), branch decision, blocks, cut lines.
2. [`docs/IMPLEMENTATION-PLAN.md`](./docs/IMPLEMENTATION-PLAN.md): every task with files, interfaces, tests, acceptance commands, subagent prompts.
3. [`docs/STRATEGY.md`](./docs/STRATEGY.md): why it wins, the judge map, the proof ladder, **claim discipline**.
4. [`docs/PLAN-B-DEVSIM.md`](./docs/PLAN-B-DEVSIM.md): the no-access substrate (verified harness contract).
5. [`docs/BUILD-SPEC.md`](./docs/BUILD-SPEC.md): the full product spec (phases, gate, playbooks, model layer).
6. [`docs/RESEARCH.md`](./docs/RESEARCH.md): verified facts. Do not re-research.
7. Ship docs: [`docs/DEMO-SCRIPT.md`](./docs/DEMO-SCRIPT.md), [`docs/SUBMISSION.md`](./docs/SUBMISSION.md), [`docs/RELIABILITY-BRIEF.template.md`](./docs/RELIABILITY-BRIEF.template.md), [`VISION.md`](./VISION.md).

## Layout
- `arga-twins-benchmark` is a symlink to the vendored clone of ArgaLabs/arga-twins-benchmark at `4a81785` (gitignored). **Never edit graders, gateway, snapshot capture or seeds.**
- `src/benchpress/` is the package. Python 3.12, pyright strict, ruff line 120. Existing: `normalize.py context.py gate.py tools.py` (61 tests).
- `devsim/` (Plan B) holds the local substrate seeded from published scenario JSON, plus the grader's trial-artifact writer.
- `tests/` covers gate, DoD rules, playbooks, replay, and devsim seed/snapshot/grader contract.
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

## State (update every session)
- 2026-09-12: specs, research, outreach drafted. Gate/tool bus/context/normalize written (61 tests). Pro ruled out.
- 2026-09-13 08:30 PT: prep session. Consolidated the main-checkout work into this worktree. Added SPRINT-PLAN, IMPLEMENTATION-PLAN, STRATEGY, DEMO-SCRIPT, SUBMISSION, RELIABILITY-BRIEF template, VISION, README, `.env.example`. Verified the harness grader contract for Plan B (see PLAN-B-DEVSIM §1). Site update: Userlens founders are now judges. Keys not yet in env, arga CLI not installed.
  **Next:** preflight (SPRINT-PLAN T-30), decide A/B at 09:25 PT, then B0 (create GitHub repo, CI).
