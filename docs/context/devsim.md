---
feature: devsim
globs:
  - devsim/**
  - tests/devsim/**
updated: 2026-09-13
---

# devsim — local twins under the unmodified ArgaBench runner (Track D)

## Current state
- Working: scaffold (`lifecycle.py` FakeArgaCli, `server.py` uvicorn port blocks, `runner.py` importlib
  `run_task` with only `SubprocessArgaCli`/`invoke_model`/`load_profile` swapped, `matrix.py` 37-profile
  copy, `candidates.py` stub/scripted, `cli.py`: serve/run/grade/report), Slack twin (calibrated to the
  CRM fixture; messages live in admin `events[]`), Gmail twin (28 tests), `benchpress_candidate.py`
  (Benchpress as `module:devsim.benchpress_candidate:benchpress`).
- Proven: stub trial → `exact_completed / valid / score_eligible`; a Benchpress smoke trial graded
  by the real grader (`unsafe: irrelevant_additive_write` — fixed in the agent).
- Done (D4): HubSpot twin (`twins/hubspot.py`, 30 tests, pyright strict, calibration notes in
  `devsim/calibration/hubspot/NOTES.md`); four of its tests run the unmodified ArgaBench canonicalizer
  and ECOM legacy grader helpers over the twin's admin state.
- WIP (pyright basic): `twins/stripe.py` (imports, SPEC, no tests).
- Missing: golden contract (D6), `baseline_candidate.py`, scored runs (D7).

## Key decisions
- Never edit the harness; swap module globals via importlib. Profiles in `devsim/profiles.json`
  (`benchpress-deepseek-v4-pro`, `baseline-deepseek-v4-pro`, flash variants).
- Seed key = scenario content sha, so ids are deterministic per task.

## Next steps
1. Finish Stripe + HubSpot twins (D5/D4 specs in `docs/AGENT-TASKS.md`).
2. D6 golden contract: scripted oracle/prospect/no-draft/sent → pass/unsafe/fail/unsafe via `devsim report`.
3. D7: `python -m devsim run --task ECOM-02 --profile benchpress-deepseek-v4-pro --candidate module:devsim.benchpress_candidate:benchpress --repeat 3`, baseline likewise; report + compare.
