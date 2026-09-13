# Sprint plan: Sunday 2026-09-13

This is the master schedule. It replaces `SUNDAY-PLAN.md`. Times are **PT / IST** (IST = PT + 12:30).
Task-level detail (files, interfaces, tests, acceptance commands) is in
[`IMPLEMENTATION-PLAN.md`](./IMPLEMENTATION-PLAN.md). The reasons behind the plan are in [`STRATEGY.md`](./STRATEGY.md).
The no-access substrate is described in [`PLAN-B-DEVSIM.md`](./PLAN-B-DEVSIM.md).

**Three rules for the day**

1. **One product, two substrates.** At 09:00 PT the only decision is which executor sits behind
   `provider_api`. Everything else is the same work.
2. **Every block ends green or cut.** At a cut line, ship what is green and disclose what is not.
   Never let one block eat the next.
3. **Commit at every checkpoint** with a message that says what now works. Judges read history.

---

## T-30: Preflight (08:30–09:00 PT / 21:00–21:30 IST)

| # | Item | Command / action | Done when |
|---|---|---|---|
| 1 | Anthropic key with ≥ $150 headroom | Add `ANTHROPIC_API_KEY=` to `.env` (copy `.env.example`) | `uv run python -c "import os;print(bool(os.environ.get('ANTHROPIC_API_KEY')))"` after `set -a; source .env` prints True |
| 2 | Arga CLI + plan check | `uv tool install arga-cli && arga login && arga whoami` | Plan tier is known. Founders or organizers may have granted access. |
| 3 | Founders reply? | Check inbox / X DMs | Yes → Plan A. No → Plan B. |
| 4 | Real-app accounts (for the usefulness segment; ≤ 10 min each, skip any that stalls) | **Stripe:** dashboard → test mode → Developers → API keys → `STRIPE_SECRET_KEY=sk_test_…`. **Slack:** api.slack.com/apps → new app in a scratch workspace → bot scopes `chat:write channels:read channels:history groups:read users:read` → install → `SLACK_BOT_TOKEN=xoxb-…`. **HubSpot:** free portal → Settings → Integrations → Private apps → scopes `crm.objects.companies.read/write crm.objects.contacts.read/write` → `HUBSPOT_PRIVATE_APP_TOKEN=pat-…`. **Gmail (optional):** OAuth Playground with scope `gmail.compose gmail.readonly` → `GMAIL_ACCESS_TOKEN` / refresh token | ≥ 3 tokens in `.env`. Slack + Stripe + HubSpot alone meet the "≥ 3 external apps" requirement. |
| 5 | Local gate green | `uv sync --group dev && uv run pytest -q && uv run ruff check . && uv run pyright` | 61 passed, 0 errors (verified 08:50 PT) |
| 6 | Harness gate green | `cd arga-twins-benchmark && uv sync --group dev && uv run pytest -q -x && uv run python scripts/build_argabench_40.py validate` | Green, or failures noted in `RESEARCH.md` |
| 7 | Screen recorder ready | QuickTime or OBS at 1920×1080, terminal font 18 pt, dark theme, notifications off | Test clip of 5 s recorded |
| 8 | Tabs open | Leaderboard `argalabs.com/benchmark`, ECOM-02 task page, DEV-03 task page, the Slack scratch workspace, Stripe test dashboard, HubSpot | — |

## 09:00–09:30 PT / 21:30–22:00 IST: Opening and **the branch**

- Attend. Ask in chat (verbatim): *"Are there Arga credits or multi-twin access for participants?
  Is pre-hackathon design work / scaffolding code allowed if disclosed? Where is the submission form?"*
- **09:25 PT: decide and do not revisit.** Multi-twin access means Plan A. Anything else means Plan B.
  Write the decision as the first line of `CLAUDE.md` "State".
- If credits arrive after 09:30 but before **12:15 PT**, add Plan A scored runs on top of Plan B.
  devsim stays: it is the cheap dev loop and the ablation substrate.
- If organizers say pre-written code is not allowed, re-type `normalize/context/gate/tools`
  from the spec in B1. Claude can regenerate them from BUILD-SPEC §7 in ~15 min. The spec is the asset.

---

## Build blocks

Roles: **Main** is you plus the main Claude session (phases, model, integration, judgment calls).
**SA-x** are subagents with isolated worktrees, dispatched with the task text from
`IMPLEMENTATION-PLAN.md`. Main reviews and merges each SA result at the checkpoint.

### B0: Repo live (09:30–09:45 PT / 22:00–22:15 IST)

| Output | Acceptance |
|---|---|
| `gh repo create rajkaria/benchpress --public --source . --push` | Repo URL opens |
| `.github/workflows/ci.yml` (uv sync, pytest, ruff, pyright) | First CI run green |
| Plan A only: fork `ArgaLabs/arga-twins-benchmark` → `rajkaria/arga-twins-benchmark`, branch `benchpress` | `git remote -v` shows the fork |
| Commit: `chore: repo, CI, decision=<A|B>` | — |

### B1: Foundations in parallel (09:45–10:45 PT / 22:15–23:15 IST)

| Lane | Tasks (IMPLEMENTATION-PLAN ids) | Output |
|---|---|---|
| Main | T1.1 `model.py`, T1.2 `prompts.py`, T1.3 `adapter.py` skeleton implementing the harness `invoke` contract | Stub adapter returns a valid `ModelInvocationResult` |
| SA-1 | **Plan B:** T1.4 devsim seed loader + stores for gmail/hubspot/slack/stripe, T1.5 `devsim/http.py` router. **Plan A:** T1.4A runner branch + two profiles in the fork | B: `uv run python -m devsim seed --task ECOM-02` prints counts equal to `seed_config`. A: `run_argabench_40.py --profile benchpress-opus-5-high --task ECOM-02` completes (grading may fail) |
| SA-2 | T1.6 playbooks `slack.py gmail.py hubspot.py stripe.py` + request-shape tests | `pytest tests/test_playbooks.py` green |

**Checkpoint 10:45 PT:** the stub adapter runs end to end on the chosen substrate and a trial
directory is written. Commit: `feat: adapter skeleton runs end-to-end on <substrate>`.
**Cut line:** if SA-1 is not green by 11:00, Main takes it over and B2 starts 15 min late. Recover
the time by cutting the CRM-02 lane in B2.

### B2: The brain, with the grader path in parallel (10:45–12:15 PT / 23:15–00:45 IST)

| Lane | Tasks | Output |
|---|---|---|
| Main | T2.1 P0 orient, T2.2 P1 policy sweep, T2.3 P2 enumerate/resolve/protected set, T2.4 P3 DoD + code rules, T2.5 P4 plan | `benchpress dry-run --task ECOM-02` prints a TaskFrame, the policy email found and quoted, the prospect inside ProtectedSet, the DoD including `unsent_customer_confirmation` + `owner_review_record`, and a plan with ≤ 24 writes |
| SA-3 | **Plan B:** T2.6 `devsim/snapshot.py` + `devsim/artifacts.py` + grader-contract test (canned PASS/UNSAFE trials) | `report_argabench_semantic_matrix.py runs/contract reports/contract` → `validity.valid == 1` for both, expected outcomes |
| SA-4 | T2.7 devsim github + linear stores (DEV-03) + playbooks `github.py linear.py` | `devsim seed --task DEV-03` counts match; playbook tests green |
| SA-5 (optional, only if B1 finished on time) | T2.8 salesforce store + playbook (CRM-02) | `devsim seed --task CRM-02` counts match |

**Checkpoint 12:15 PT / 00:45 IST:** dry run is correct on ECOM-02 **and** the grader accepts a
devsim trial dir. Commit: `feat: P0–P4 find policy, lock look-alikes, emit DoD + plan`.
**Cut lines:**
- Grader path not green by 12:15 → switch to `PLAN-B-DEVSIM.md` §6 (own assertions, disclosed). Keep moving.
- P1 not finding the policy email by 12:00 → hard-code nothing. Widen the P1 scan (read every Gmail message, cap 25) and move on.
- SA-4 late → DEV-03 becomes a gate-replay-only story (rung 6). ECOM-02 carries the graded runs.

### B3: Hands and proof (12:15–13:15 PT / 00:45–01:45 IST)

| Lane | Tasks | Output |
|---|---|---|
| Main | T3.1 P5 execute through gate + read-back, T3.2 P6 verify + one repair round, T3.3 P7 deliverables + final JSON | First full ECOM-02 trial graded. Iterate until PASS, or until 13:15. |
| SA-6 | T3.4 baseline runner: the stock `AnthropicMessagesAdapter` on the same substrate. T3.5 `scripts/bp_compare.py` | Baseline ECOM-02 trial graded. Compare table renders from two reports. |
| SA-7 | T3.6 `report.py` receipt JSON → `receipt.html` (single file, dark, per DEMO-SCRIPT visual spec) | Receipt opens in a browser from a real trial dir |

**Checkpoint 13:15 PT / 01:45 IST:** ECOM-02 graded (target PASS). **Feature freeze on agent
logic** except fixes for graded failures. Commit: `feat: P5–P7 — gated execution, read-back, deliverables; ECOM-02 <outcome>`.
**Mid-sprint judge sim (5 min, subagent):** run the STRATEGY §7 panel on the repo as it stands.
Take only the top 2 fixes that fit B4.

### B4: Scored runs and usefulness segment (13:15–14:30 PT / 01:45–03:00 IST)

Run first, then work while trials execute.

| Lane | Tasks | Output |
|---|---|---|
| Runs (background) | T4.1 Benchpress ECOM-02 × 3, DEV-03 × 3 · baseline ECOM-02 × 3, DEV-03 × 3 (drop to × 1 if cost or time is tight) · CRM-02 × 3 if SA-5 landed | `runs/…` → `reports/…` semantic reports committed |
| Runs (background) | T4.2 ablations × 1: `--no-policy-sweep` (ECOM-02), `--no-gate` (DEV-03), `--no-readback` (ECOM-02) | `reports/ablations/*.json` |
| Main | T4.3 gate replay of baseline unsafe calls ($0). T4.4 `realapp.py` executor + a real run on Slack + Stripe test + HubSpot (+ Gmail) | `reports/gate-replay.md`; a real receipt; screenshots |
| SA-8 | T4.5 fidelity check: same playbook ops on devsim vs real apps, shape diff | `reports/fidelity.md` |

**Checkpoint 14:30 PT / 03:00 IST: code freeze.** Commit reports. From here on, only docs, the
video and fixes for crashes.
**Cut lines:** real-app wiring not done by 14:15 → film the devsim run and the receipt, with the
on-screen line "local twin substrate, same APIs". Trials still running at 14:30 → report the
completed ones, say how many ran, never extrapolate.

### B5: Story (14:30–15:10 PT / 03:00–03:40 IST)

| Output | Source |
|---|---|
| `docs/RELIABILITY-BRIEF.md` (≤ 2 pages; PDF export) | Fill `RELIABILITY-BRIEF.template.md` from `reports/`. Every number cites a file. |
| `README.md` results table + GIF/screenshot of receipt | `reports/compare.md` |
| `VISION.md` final pass | Already drafted |
| Final judge sim (subagent, 7 judges) → fix only docs and claims | STRATEGY §7 personas |
| Commit: `docs: reliability brief, results, vision` | — |

### B6: Video (15:10–15:45 PT / 03:40–04:15 IST)

Record to [`DEMO-SCRIPT.md`](./DEMO-SCRIPT.md). Two takes max. Voice first (script read aloud,
1:55), then screen captures laid to it. Upload to YouTube **unlisted** by 15:40. Put the link in
the README.

### B7: Submit (15:45–15:55 PT / 04:15–04:25 IST, **hard stop 15:55**)

Follow `SUBMISSION.md` checklist. Screenshot the confirmation. Final `git push`. Tag `v0.1.0-hackathon`.

### Judging (16:00–16:40 PT / 04:30–05:10 IST)

Have open: receipt.html (ECOM-02), `reports/compare.md`, gate replay, the brief, SUBMISSION.md Q&A.

---

## Plan A deltas (only if multi-twin access is granted)

| Block | Change |
|---|---|
| B1 SA-1 | Fork + runner branch + profiles (T1.4A) instead of devsim stores. `build_argabench_40.py stage` then `seed-check --task ECOM-02 --task DEV-03`. Record provision time. |
| B2 SA-3/SA-4 | Not needed. SA-3 → tier C prep (CRM-01, ECOM-08, IT-06 playbooks: salesforce, jira). SA-4 → github/linear playbooks only. |
| B4 | Official harness: `run_argabench_40.py --profile benchpress-opus-5-high` × 3 and `--profile opus-5-high` × 1. Start by 13:30 PT at the latest because twin provisioning is slow. `--concurrency 4`. |
| Claims | "Ran inside ArgaBench on Arga twins, graded by `argabench_fair`." Leaderboard comparison is legitimate. |

## Failure playbook

| Symptom | Action |
|---|---|
| Grader: `reviewed_unsent_confirmation` fail | Draft body must include entity + ≥ 2 facts. The Slack review message must contain "review" + "owner" + one fact. Fix P7 templates and rerun that task only. |
| Grader: `unsafe` protected/distractor changed | Check the ledger for non-GET calls on protected ids. Tighten gate `hit()`. Add the regression case to `test_gate.py`. |
| Grader: `structured_result` fail | A fact is missing from the final JSON. P7 must copy every DoD fact and every acted-on identifier. |
| Grader: `slack_update` fail | The message went to the wrong channel id, or no fact was mentioned. Resolve channel name → id in P0. |
| Trial `invalid_*` (not score-eligible) | The artifact contract is broken. Diff the trial dir against the SA-3 canned PASS dir. |
| Over-refusal (escalated on a clear target) | Resolution prompt: decisive when exactly one candidate matches lifecycle + domain. Escalate only on a true evidence tie. |
| Tool budget exceeded | Lower P1/P2 caps by 10 each (`tools.py` PHASE_BUDGETS). |
| Anthropic 429/529 | Backoff is in `model.py`. Drop run concurrency to 2. Switch dev iteration to Sonnet 5. |
| Model returns invalid schema twice | Conservative default (escalate). Log it. Count it in the brief. |
| Laptop/network dies during B4 | Trials are resumable per task. Rerun only missing trial dirs. |

## Energy

Eat at 12:15 PT (00:45 IST) while the SA-3/SA-4 results are reviewed. Stand up at every
checkpoint. The session limit is real: at the 13:15 checkpoint run `/save-context` and start a
fresh Claude session with `CLAUDE.md` + this file. Context bloat will slow the afternoon.
