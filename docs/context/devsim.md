---
feature: devsim
globs:
  - devsim/**
  - tests/devsim/**
updated: 2026-09-14
---
# devsim — local twins under the unmodified ArgaBench runner (Track D)

## Current state — what's working
- All four twins finished and strict (Slack, Gmail, HubSpot, Stripe; 175+ twin tests). `devsim serve --empty`
  starts unseeded twins for `evals.run`/`evals.contract`; `devsim run` spins its own twins per trial.
- Candidates: `devsim/benchpress_candidate.py`, `devsim/baseline_candidate.py`.
- **Results under the unmodified ArgaBench semantic report:** Benchpress **3/3 pass**, baseline 0/3 fail
  (`reports/devsim/<arm>/repeat-NN/semantic-report.json`).

## Recent changes — files touched and why
- `devsim/twins/gmail.py`: `drafts.create` echoes `format=full` (headers + snippet). The legacy grader reads task
  facts from call text and cannot decode base64, so a minimal echo (real Gmail's shape) can never satisfy
  `reviewed_unsent_confirmation` or bind the draft to the target. Documented in the code; add to calibration notes.
- `devsim/cli.py`: `serve --empty`. `devsim/baseline_candidate.py`: new.

## Key decisions
- Never edit the harness; calibrate twins to what the graders can observe, and say so in the brief (§5/§7).
- Harness runs use `BENCHPRESS_MAX_TOKENS=16000` exported in the shell (devsim CLI does not load `.env`).

## Next steps
1. D6 golden contract on devsim (scripted oracle/prospect/no-draft/sent → pass/unsafe/fail/unsafe via `devsim report`).
2. Add the `drafts.create` echo decision to `devsim/calibration/gmail/NOTES.md`.
3. More repeats (5×) per arm if credits allow.
