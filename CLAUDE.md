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

## State (update every session)
- **2026-09-13 10:05 PT checkpoint.** Merged: model layer (DeepSeek live), controller + phases P0–P7 + adapter + receipt + CLI (replay test green), playbooks (slack/gmail/hubspot/stripe), real-app gateway + Slack/Stripe/HubSpot/Gmail seeders + `evals.seed`, scenarios + harness bridge, devsim scaffold (unmodified harness `run_task` → stub twins → `exact_completed/score_eligible`), devsim Slack twin. Live: DeepSeek key + Stripe test key in `.env`; ECOM-02 Stripe slice seeded in the real test account (`runs/live/stripe-manifest.json`) and the Stripe playbook resolves both look-alikes through the real gateway. In flight (agent branches `worktree-agent-*`): gmail/hubspot/stripe twins, assertions + contract, run loop + baseline + compare + gate replay, receipt HTML. Still needed from Raj: HubSpot private-app token, Slack bot token, Gmail OAuth (client id/secret/refresh token + GMAIL_ADDRESS).

- **2026-09-13 09:30 PT — build decision (final, do not revisit):** substrate = real apps (Plan B) with devsim local twins as the deterministic eval/rehearsal substrate (Track D, own agents); model = DeepSeek (`deepseek-chat` via the OpenAI-compatible API) for the hackathon, Anthropic after. Organizers at the opening: no Arga credits; pre-written code allowed; Arga/Lemma use not required; what matters is real-world usefulness, ≥3 external apps connected, a critical workflow. Framing: Benchpress is an ops agent for customer-account changes under a review policy (billing-contact / renewal-contact changes from Slack → HubSpot + Stripe → Gmail draft for owner review → Slack receipt); ArgaBench is the evidence, not the product. Repo public at github.com/rajkaria/benchpress (main).
- 2026-09-12: specs, research, outreach drafted. Gate/tool bus/context/normalize written (61 tests). Pro ruled out.
- 2026-09-13 08:30 PT: prep session. Consolidated the main-checkout work into this worktree. Added SPRINT-PLAN, IMPLEMENTATION-PLAN, STRATEGY, DEMO-SCRIPT, SUBMISSION, RELIABILITY-BRIEF template, VISION, README, `.env.example`. Audited the harness (975 tests green). **Plan B changed:** devsim + official grader costs 12–16 h per task, so Plan B is now a real-app rehearsal eval (PLAN-B.md), and the grader-faithful rebuild runs in parallel as Track D (TRACK-DEVSIM.md, own session/agents, 14:00 PT gate). Plan A must not touch canonical `model_matrix.json` (23 tests break) and needs an `agent` kwarg. Site update: Userlens founders are now judges. Keys not yet in env, arga CLI not installed, real-app accounts not yet created.
  **Next:** preflight (SPRINT-PLAN T-30), decide A/B at 09:25 PT, then B0 (create GitHub repo, CI).
