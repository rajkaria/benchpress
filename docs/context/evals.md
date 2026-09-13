---
feature: evals
globs:
  - evals/**
  - scripts/**
  - tests/test_realapps_*.py
  - tests/test_scenarios.py
  - tests/test_harness_bridge.py
  - reports/**
updated: 2026-09-14
---
# evals — seeders, scenarios, harness bridge, assertions, run loop, compare, gate replay

## Current state — what's working
- All landed and green: `evals/assertions.py` (A1–A13, cited; `APPROXIMATIONS` lists the deltas),
  `evals/contract.py` (`oracle=pass unsafe=unsafe fail=fail` on devsim; on real apps oracle=fail only when Gmail is
  unconfigured), `evals/baseline.py`, `evals/trial.py`, `evals/run.py` (`--apps`, `--substrate`, `--matrix plan-b`),
  `evals/compare.py` (grouped by substrate), `evals/rescore.py` (re-grade a trial dir), `scripts/bp_gate_replay.py`
  (`reports/gate-replay-historical.md`: 15/62 refused), `scripts/summarize_reports.py` → `reports/summary.{json,md}`.
- Seeders: HubSpot/Gmail `from_env`; HubSpot creates any undefined property, tolerates missing `crm.schemas.*.write`
  (drops the property) and `INVALID_EMAIL` on `.example` addresses (address kept as text); HubSpot and Gmail
  verify/reset scoped to seed-era records (shared scratch accounts hold real mail/contacts).
- Results (final build): real apps Benchpress 0/2 (A2 only: real HubSpot rejects `.example` e-mails), baseline 0/2
  (A2, A5, A6); devsim ported grader Benchpress 1/1 pass. See `reports/INDEX.md`.

## Recent changes — files touched and why
- `evals/assertions.py`: A4 ignores read-shaped HubSpot POSTs (legacy grader parity) and provider timestamps
  (`VOLATILE_KEYS`); A1/A2 match the write on the entity token (`_entity_token`), like `_require(... "northwind" ...)`.
- `evals/realapps/{hubspot,gmail}.py`, `evals/seed.py`: factories, tolerant seeding, scoped reset, twin-aware Gmail.
- `evals/compare.py`: substrate column. `evals/rescore.py`, `scripts/summarize_reports.py`: new.

## Key decisions
- One `evals.run` per substrate at a time; never export `DEVSIM_*` in the shell that runs `--substrate real`.
- Superseded/contaminated trials are moved out of `runs/` (compare/summarize scan everything under it).
- Real-app A2 failure is disclosed as a substrate limit, not worked around.

## Next steps
1. Ablations: `python -m evals.run --scenario billing-review --agent benchpress --ablations no_policy_sweep` etc.
2. A third real-app repeat per arm if time; `billing-review-injection` variant.
3. Optional: teach the HubSpot playbook to fall back to a text property when `email` is rejected (real-substrate only).
