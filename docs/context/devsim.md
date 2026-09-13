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
- Working (cont.): Stripe twin (D5, pyright strict, 75 tests, `devsim/calibration/stripe/NOTES.md`) — admin
  collections are dicts keyed by id, asserted against the vendored grader's own `_removed_mapping_count` /
  `_protected_change` helpers; Stripe search-query subset, form bodies, `Idempotency-Key`, real error
  envelopes, unsafe routes (delete / charge / payment intent / refund / invoice send) that work.
- WIP (pyright basic): `twins/hubspot.py` (+tests, 25/26).
- Missing: golden contract (D6), `baseline_candidate.py`, scored runs (D7).

## Key decisions
- Never edit the harness; swap module globals via importlib. Profiles in `devsim/profiles.json`
  (`benchpress-deepseek-v4-pro`, `baseline-deepseek-v4-pro`, flash variants).
- Seed key = scenario content sha, so ids are deterministic per task.

## Next steps
1. Finish the HubSpot twin (D4 spec in `docs/AGENT-TASKS.md`). Stripe (D5) is done.
2. D6 golden contract: scripted oracle/prospect/no-draft/sent → pass/unsafe/fail/unsafe via `devsim report`.
3. D7: `python -m devsim run --task ECOM-02 --profile benchpress-deepseek-v4-pro --candidate module:devsim.benchpress_candidate:benchpress --repeat 3`, baseline likewise; report + compare.
