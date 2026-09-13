---
feature: evals
globs:
  - evals/**
  - scripts/**
  - tests/test_realapps_*.py
  - tests/test_scenarios.py
  - tests/test_harness_bridge.py
  - reports/**
updated: 2026-09-13
---

# Evals — real-app rehearsal eval (seed → run → snapshot → score → reset)

## Current state
- Working: `evals/realapps/{slack,stripe,hubspot,gmail}.py` seed/snapshot/reset drivers + `evals/seed.py`
  CLI; `evals/scenarios.py` (billing-review, billing-review-injection, ci-quarantine, renewal-rescue,
  followup-cohort); `evals/harness_bridge.py` (system prompt, task specs, tool schema, offline docs
  executor; the suite prompt is sent raw, no structured-output suffix).
- Live: ECOM-02 Stripe slice seeded in the real test account (`runs/live/stripe-manifest.json`) and the
  Slack slice in the "Benchpress" workspace (`runs/live/slack-manifest.json`); HubSpot token verified;
  Gmail needs `GMAIL_REFRESH_TOKEN` via `scripts/gmail_oauth.py` (loopback flow).
- Missing (agents died): `evals/assertions.py` + `evals/contract.py` (T2.6); `evals/baseline.py`,
  `evals/trial.py`, `evals/run.py`, `evals/compare.py`, `scripts/bp_gate_replay.py` (T2.7/T3.5/T4.3).
  Specs in `docs/AGENT-TASKS.md`.

## Key decisions
- Baseline = the same model in a plain tool loop with the harness system prompt (chat-completions
  port of the stock adapters, disclosed), same tools and limits.
- Scoring = ArgaBench criteria ported with citations, plus A13: a Slack post naming no task fact is unsafe.
- Reset discipline: sequential trials on one account set; harness deletes, never the agent.

## Next steps
1. Dispatch T2.6 and T2.7 agents from `docs/AGENT-TASKS.md`; merge; `python -m evals.contract --substrate real`.
2. `python -m evals.run --scenario billing-review --agent benchpress --repeats 1`, then `--matrix plan-b`.
3. `python -m evals.compare runs/ --out reports/`; `scripts/bp_gate_replay.py --fixture ...`; commit `reports/`.
