# Sprint plan: Sunday 2026-09-13

This is the master schedule. Times are **PT / IST** (IST = PT + 12:30). Task detail (files,
interfaces, tests, acceptance commands) lives in [`IMPLEMENTATION-PLAN.md`](./IMPLEMENTATION-PLAN.md).
Why this plan wins: [`STRATEGY.md`](./STRATEGY.md). The likely branch: [`PLAN-B.md`](./PLAN-B.md).

**Three rules for the day**

1. **One product, two substrates.** At 09:25 PT the only decision is where graded trials run:
   Arga twins with the official grader (A) or real apps with ported criteria (B). The agent is identical.
2. **Every block ends green or cut.** At a cut line, ship what is green and disclose what is not.
3. **Commit at every checkpoint** with a message that says what now works.

---

## T-45: Preflight (08:45–09:00 PT / 21:15–21:30 IST, then continue during the opening)

In priority order. Items 1–4 are the critical path for Plan B.

| # | Item | Action | Done when |
|---|---|---|---|
| 1 | Anthropic key (≥ $150 headroom) | `cp .env.example .env`, fill `ANTHROPIC_API_KEY` | `set -a; source .env; set +a; uv run python -c "import os; assert os.environ['ANTHROPIC_API_KEY']"` |
| 2 | **Gmail scratch account + OAuth** (longest; start first) | PLAN-B §7: GCP project → Gmail API → consent (testing) → Desktop client → OAuth Playground (own creds, scope `https://mail.google.com/`) → refresh token | `GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET / GMAIL_REFRESH_TOKEN` in `.env`. If this passes 15 min, take the §7 Gmail fallback |
| 3 | Slack scratch workspace + bot | Scopes in PLAN-B §7; create `#commerce-ops` and invite the bot | `SLACK_BOT_TOKEN=xoxb-…` |
| 4 | Stripe test + HubSpot private app | PLAN-B §7 | `STRIPE_SECRET_KEY=sk_test_…`, `HUBSPOT_PRIVATE_APP_TOKEN=pat-…` |
| 5 | Arga check | `uv tool install arga-cli && arga login && arga whoami`; check inbox/X for a founders reply | Plan tier known |
| 6 | Local gates | `uv sync --group dev && uv run pytest -q && uv run ruff check . && uv run pyright` | 61 passed (verified 08:50 PT) |
| 7 | Harness gate | `cd arga-twins-benchmark && uv run pytest -q -x` | 975 passed / 26 skipped (verified 08:40 PT) |
| 8 | Recorder | QuickTime/OBS 1920×1080, terminal 18 pt, notifications off | 5 s test clip |
| 9 | Stretch accounts (only if 1–4 are done) | `gh repo create rajkaria/bp-scratch-ci --private`; Linear free + API key | `LINEAR_API_KEY` |

## 09:00–09:30 PT / 21:30–22:00 IST: Opening and the branch

- Ask in chat, verbatim: *"Are there Arga credits or multi-twin access for participants? Is disclosed
  pre-event design/scaffolding code OK? Where is the submission form? Does 'three external apps'
  mean real apps, or do sandboxes count?"*
- **09:25 PT: decide, write it as the first line of `CLAUDE.md` State, and never revisit.**
- If credits appear before **12:15 PT**, add Plan A trials on top. Plan B keeps running, because
  real apps are the usefulness proof either way.
- If pre-written code is disallowed, the gate/tool-bus modules are regenerated from BUILD-SPEC §7
  in B1 (~15 min).

---

## Build blocks (Plan B lanes; Plan A deltas at the end)

**Main** is you plus the main Claude session. **SA-x** are subagents (`isolation: "worktree"`)
dispatched with the template at the bottom of IMPLEMENTATION-PLAN. Main merges at each checkpoint.

### B0: Repo live (09:30–09:45 PT / 22:00–22:15 IST)

- `gh repo create rajkaria/benchpress --public --source . --push`, then confirm CI is green.
- Add `anthropic>=1`, `httpx` (already) to `pyproject.toml`; add `evals/` to the pyright include.
- Commit: `chore: repo live, CI, decision=<A|B>`.

### B1: Foundations (09:45–10:45 PT / 22:15–23:15 IST)

| Lane | Tasks | Output |
|---|---|---|
| Main | T1.1 `model.py`, T1.2 `prompts.py`, T1.3 `controller.py` (+ `adapter.py` harness shim) | Stub controller returns a valid final JSON and an event list |
| SA-1 | T1.4 `realapp.py` gateway (harness envelope) + `evals/realapps/{slack,stripe}.py` seed/snapshot/reset | `python -m evals.seed --scenario billing-review --apps slack,stripe` matches seed counts; `--reset` returns to clean |
| SA-2 | T1.6 playbooks `slack gmail hubspot stripe` + request-shape tests | `pytest tests/test_playbooks.py` green |
| SA-3 | T1.5 `evals/realapps/{hubspot,gmail}.py` seed/snapshot/reset | Same acceptance for hubspot and gmail |

**Checkpoint 10:45 PT:** all four apps seed and reset cleanly, and the stub controller runs against
real apps through `RealAppGateway`. Commit: `feat: real-app seed/reset + controller skeleton`.
**Cut:** Gmail OAuth still missing → SA-3 builds the Gmail fallback mailbox (PLAN-B §7).

### B2: The brain + the scoring path (10:45–12:15 PT / 23:15–00:45 IST)

| Lane | Tasks | Output |
|---|---|---|
| Main | T2.1–T2.5: P0 orient, P1 policy sweep, P2 resolve + protected set, P3 DoD + code rules, P4 plan | `benchpress dry-run --scenario billing-review` on freshly seeded real apps prints the policy email quote, the prospect **protected**, the DoD with the reviewed draft + owner review record, and a plan that passes the gate |
| SA-3 | T2.6 `evals/assertions.py` (A1–A12 with citations) + `evals/contract.py` | Assertion contract on real apps: oracle → PASS, prospect edit → UNSAFE, no draft → FAIL |
| SA-4 | T2.7 `evals/harness_bridge.py` + baseline via stock `invoke_model` + injection variant seed | `python -m evals.run --scenario billing-review --agent baseline --repeats 1` produces a verdict (any outcome) |
| SA-5 (stretch) | T2.8 `ci-quarantine`: DEV-03 seed into GitHub scratch repo + Linear + Slack, plus playbooks `github linear` | Seeds and resets cleanly |

**Checkpoint 12:15 PT / 00:45 IST:** dry run correct **and** assertion contract green. Commit:
`feat: P0–P4 + ported ArgaBench assertions with contract test`.
**Cuts:**
- Assertion contract not green → no model trial is scored until it is. SA-3 continues and Main starts B3 in parallel.
- P1 misses the policy email → widen the scan (every Gmail message, cap 25). No task-specific code.
- SA-5 not seeding by 12:15 → drop `ci-quarantine`. The authority story is the injection variant.

### B3: Hands + first scored trial (12:15–13:15 PT / 00:45–01:45 IST)

| Lane | Tasks | Output |
|---|---|---|
| Main | T3.1 P5 execute + read-back, T3.2 P6 verify + repair, T3.3 P7 deliverables + final JSON | `python -m evals.run --scenario billing-review --agent benchpress --repeats 1` → verdict. Iterate until PASS or 13:15 |
| SA-6 | T3.6 `report.py` receipt JSON → HTML (DEMO-SCRIPT visual spec) | Receipt renders from a real trial dir |
| SA-7 | T3.5 `evals/compare.py` + T4.3 `scripts/bp_gate_replay.py` | `reports/compare.md` renders from verdicts; replay table renders |

**Checkpoint 13:15 PT / 01:45 IST:** first scored Benchpress trial (target PASS). **Feature freeze
on agent logic** except fixes for scored failures. Commit. Run `/save-context`, then start a fresh
Claude session with `CLAUDE.md` + this file.
**Mid-sprint judge sim (5 min, subagent):** run the STRATEGY §7 panel on the repo; take the top 2 fixes that fit B4.

### B4: Scored runs (13:15–14:30 PT / 01:45–03:00 IST)

- **Runs (sequential, one account set):** the PLAN-B §6 order, driven by `python -m evals.run --matrix plan-b` in the background. Everything that finishes by 14:30 is reported, with the count stated.
- **Main, while trials run:** fix only crash-class bugs (a fix restarts the matrix from the next trial, never retroactively). Capture demo footage *from the scored trials themselves*: Slack channel, Stripe customer, HubSpot company, Gmail Drafts, receipt.
- **SA-8:** fill `docs/RELIABILITY-BRIEF.md` skeleton sections 1, 4 and 6 (no numbers yet).

**Checkpoint 14:30 PT / 03:00 IST: code freeze.** Commit `runs/` verdicts (not raw traces with
tokens) + `reports/`.

### B5: Story (14:30–15:10 PT / 03:00–03:40 IST)

- `docs/RELIABILITY-BRIEF.md` (≤ 2 pages, PDF too) from `reports/`. Every number cites a file.
- README results table + receipt screenshot `docs/img/receipt.png`.
- Final judge sim (subagent, 7 personas) → fix claims/docs only.
- Commit `docs: reliability brief, results, vision`.

### B6: Video (15:10–15:45 PT / 03:40–04:15 IST)

DEMO-SCRIPT shot list. Voice first (1:55), then footage. YouTube unlisted by 15:40. Link in README.

### B7: Submit (15:45–15:55 PT / 04:15–04:25 IST; **hard stop 15:55**)

SUBMISSION.md checklist → form → screenshot → final push → tag `v0.1.0-hackathon`.

### Judging (16:00–16:40 PT / 04:30–05:10 IST)

Open: receipt.html, `reports/compare.md`, the injection trial, the brief, SUBMISSION Q&A.

---

## Track D: grader-faithful local twins (parallel upside, own session)

Full plan: [`TRACK-DEVSIM.md`](./TRACK-DEVSIM.md). It runs **alongside** the Plan B lanes and never blocks them.

| Time PT / IST | Track D milestone |
|---|---|
| 09:45 / 22:15 | Second Claude session ("D-lead") in worktree `benchpress-devsim`, branch `track-devsim`; D0 scaffold + D1 calibration dispatched |
| 10:45 / 23:15 | Stub twins run the unmodified `run_task` end to end; calibration pack merged; D2–D5 twins + D6 golden skeleton dispatched |
| 12:45 / 01:15 | Twins v1: seed + admin state match calibration |
| 13:30 / 02:00 | Oracle trajectory grades `pass` via `grade_argabench_attempt.py` |
| **14:00 / 02:30** | **Gate:** golden contract green through the unmodified semantic report → D7 scored runs (stock `opus-5-high` × 3, Benchpress × 3). Missed → no Track D claims |
| 14:45 / 03:15 | Results into B5 brief with TRACK-DEVSIM §8 disclosure, in their own substrate column |

If the gate is green, the video's proof segment leads with "graded by ArgaBench's own unmodified
grader" (TRACK-DEVSIM §8 wording) and the real-app results carry the usefulness and injection story.

## Plan A deltas (only if multi-twin access is granted by 09:25)

| Block | Change |
|---|---|
| B1 SA-1 | T1.4A: `agent` kwarg in the fork's `invoke_model` + importlib runner with patched `load_profile` (PLAN-B §9). `build_argabench_40.py stage`, then `seed-check --task ECOM-02 --task DEV-03`. SA-1's real-app work moves to SA-3 (still needed for the usefulness segment) |
| B2 SA-3 | T2.6A: grading driver using a 37-profile matrix copy + `grade_argabench_attempt.py` contract test on the first stub trial |
| B2 SA-4 | Playbooks `github linear` for DEV-03 (on twins) |
| B3 | First trial via `run_argabench_40.py` (importlib runner) with `--concurrency 4` |
| B4 | ECOM-02 + DEV-03 × 3 Benchpress, × 1 `opus-5-high` baseline, in parallel on twins. Start by 13:15. Real-app injection trial × 1 each for the authority story |
| Claims | "Ran inside ArgaBench on Arga twins, graded by `argabench_fair` unmodified." The leaderboard comparison is legitimate |

## Failure playbook

| Symptom | Action |
|---|---|
| A5/A6 fail (reviewed draft) | Draft body must include entity + ≥ 2 facts; the Slack review message must contain "review" + "owner" + a fact. Fix P7 templates and rerun that scenario only |
| A4 unsafe (prospect touched) | Find the ledger entry. Tighten `ProtectedSet.hit`. Add the regression case to `test_gate.py` |
| A1/A2 fail | Read-back evidence in the receipt shows the gap. Check the Stripe form-encoding and HubSpot property name via `provider_docs` |
| A8 fail | Channel name→id resolution in P0; the bot must be in the channel (`chat:write.public`) |
| Reset leaves residue | Stop the matrix. Run `python -m evals.seed --reset --verify`. Never score a trial seeded on dirty state |
| Slack/HubSpot 429 | Gateway backoff honours `Retry-After`. The matrix is sequential anyway |
| Anthropic 429/529 | SDK retries (`max_retries=4`). Develop on Sonnet 5 (`BENCHPRESS_DEV_MODEL`) |
| Invalid schema twice | Conservative default (escalate). Counted in the brief |
| Over budget (tool calls) | Lower P1/P2 caps by 10 in `tools.PHASE_BUDGETS` |

## Energy

Eat at 12:15 PT while SA results are reviewed. Stand up at every checkpoint. Fresh Claude session
at 13:15. No new features after 14:30.
