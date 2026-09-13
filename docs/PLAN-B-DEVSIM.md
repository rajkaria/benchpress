# Plan B — devsim: grading ourselves with ArgaBench's own verifier, without Arga twins

**Trigger:** no multi-twin Arga access by 08:30 PT Sunday (no founder reply, no hackathon
credits from the organizers at the 09:00 opening, and we are not buying Pro at $1,250/mo).

**Claim discipline, before anything else.** Under Plan B we never say "we passed ArgaBench."
We say, in the video, the README and the brief:

> Graded by ArgaBench's published `argabench_fair` verifier against a locally reconstructed
> ECOM-02 environment. Not Arga twins — Arga's Free plan allows one twin per run and the
> task provisions four, so the scenario was rebuilt from the published seed and run against
> real apps. Same seed, same grader, different substrate.

Akira Tong wrote this harness. A blurred claim costs more than the result is worth, and the
honest version is itself the reliability story the 25% criterion is asking for.

---

## 1. Why this is possible (verified 2026-09-12 against vendored HEAD `4a81785`)

| Fact | Evidence |
|---|---|
| The full seed for every task is public and declarative | `benchmark/argabench_40/scenarios/ecom-02.json` — 10,313 bytes, keys `description name seed_config tags twins`; `twins: ["gmail","hubspot","slack","stripe"]`; `seed_config` is one dict per provider with literal records (Gmail `messages`/`labels`/`drafts`, etc.) |
| The grader never contacts Arga | `scripts/report_argabench_semantic_matrix.py` takes `matrix_dir` + repo-local `suite.json`, `TASKS.md`, `model_matrix.json`, `historical_…_calibration.json`. No network, no twin handles. |
| Grading reads a fixed set of JSON artifacts | `attempt.json control.json invocation.json provider-trace.json official-docs-trace.json tool-steps.json baseline-state.json final-state.json raw-state-diff.json cleanup.json run-config.json` |
| State snapshots are declarative and strictly validated | `evaluation/state_capture.py`: `TrustedStateSnapshot` serializes to exactly `{"providers": …, "queries": …}`; `from_artifact` raises `StateCaptureError` on any other top-level key. Assertions cite pointers like `/queries/<id>/body`. |
| A trial only scores when it classifies as `exact_completed` and the domain grade is not `evidence_gap` | `reporting/argabench_semantic_report.py:1385-1475` — otherwise `invalid_infrastructure` / `invalid_grader`, excluded from scoring |

So the verifier is a pure function over a directory of JSON. If devsim writes that directory
faithfully, the judges' own grader scores our run on our laptop, for $0.

**Not yet verified — check first thing Sunday (30 min, before building):**

- How `execution_class` is computed, and the exact fields in `attempt.json` / `invocation.json`
  it keys on.
- Where `SnapshotQuerySpec` values come from per task (suite.json? task spec?) and the exact
  query ids ECOM-02's assertions dereference.
- Whether `raw-state-diff.json` must be a real diff or may be derived from baseline/final.
- Whether `cleanup.json` / `control.json` presence is required for `exact_completed`.

If any of these turn out to be twin-coupled in a way we cannot honestly reproduce, stop and
fall back to §6.

## 2. What devsim is

`devsim/` is a local, deterministic stand-in for the twin fleet, plus an artifact writer.

```
devsim/
  seed.py       # load benchmark/argabench_40/scenarios/<task>.json -> provider stores
  providers/    # gmail.py hubspot.py slack.py stripe.py github.py linear.py salesforce.py
  http.py       # routes provider_api {method,path,body} -> store mutation/read, same shape as the gateway
  snapshot.py   # stores -> TrustedStateSnapshot artifact {"providers":…,"queries":…}
  artifacts.py  # write the full trial directory the grader expects
  run.py        # CLI: devsim run --task ECOM-02 --profile benchpress-opus-5-high --repeats 3
```

The **agent does not change**. `src/benchpress/` runs byte-identical; only the `execute_tool`
executor is swapped, exactly as `realapp.py` already does (BUILD-SPEC §13.1). That equivalence
is the point, and it is what makes the local result meaningful rather than theatre.

Two substrates, same adapter:

- **devsim** (default): in-process stores seeded from the published scenario. Deterministic,
  instant, free, repeatable. This is what we grade.
- **real apps** (`--real`): Slack scratch workspace, Stripe **test mode**, HubSpot free portal,
  Gmail OAuth on a scratch account, GitHub, Linear. All free tiers. This is what we film.

## 3. Build order (≈3h, and it is mostly plumbing)

| # | Step | Box | Done when |
|---|---|---|---|
| 1 | Verify the four open questions in §1 | 0:30 | Written into this file |
| 2 | `seed.py` + stores for gmail/hubspot/slack/stripe (ECOM-02's four) | 0:45 | `devsim seed --task ECOM-02` prints record counts matching `seed_config` |
| 3 | `http.py` — the `provider_api` surface the playbooks already call | 0:45 | Existing playbook unit tests pass against devsim with no playbook edits |
| 4 | `snapshot.py` | 0:30 | `TrustedStateSnapshot.from_artifact(json.load(...))` round-trips without raising |
| 5 | `artifacts.py` + `run.py` | 0:30 | `report_argabench_semantic_matrix.py runs/devsim-r1 reports/devsim-r1` exits 0 with `validity.valid == 1` |
| 6 | Add salesforce (CRM-02, CRM-05) | 0:30 | Both tasks grade end to end |

Step 5's exit criterion is the real gate: **a trial the published grader calls `valid` and
`score_eligible`, whatever the outcome.** A graded FAIL on Sunday morning is a success for
devsim; it means the loop is honest and the agent has something to beat.

## 4. Verification (this is the submission's spine, so it gets tests)

- `tests/test_devsim_seed.py` — every record in `scenarios/ecom-02.json` is queryable through
  `provider_api` after seeding; no extra records invented.
- `tests/test_devsim_snapshot.py` — snapshot artifact satisfies `TrustedStateSnapshot.from_artifact`;
  fails loudly if the harness's schema moves.
- `tests/test_devsim_grader_contract.py` — a canned PASS trial and a canned UNSAFE trial both
  grade to the expected `semantic_outcome`. This is the test that proves we are not grading
  ourselves generously.
- Existing `test_gate.py` / `test_playbooks.py` / `test_phases_replay.py` run unchanged against
  devsim.

## 5. What this does to the submission

| Criterion | Weight | Effect |
|---|---|---|
| Technical execution | 30% | None. The adapter, gate, phases, read-back and tests are all local anyway. Devsim *adds* engineering surface. |
| Reliability & evaluation | 25% | Neutral to positive. We show a seeded eval, N repeats, a pass/fail/unsafe taxonomy, replay tests, and grading by a third party's published verifier rather than our own rubric. |
| Usefulness | 20% | Up. "Connect to at least three external apps" reads stronger on real Slack + Stripe + HubSpot + Gmail than on sandboxes. |
| Originality | 15% | Small loss. "Shipped inside your benchmark" becomes "rebuilt your hardest scenarios and ran your grader." Still unusual; less striking. |
| Demo clarity | 10% | None. The video still opens on the public leaderboard showing 0/111 — that data is public. |

Net: Plan B is a **weaker pitch and an equal submission**. The thing it costs is the sentence
"we ran inside ArgaBench and passed a task no frontier model has passed." The thing it protects
is everything the score is actually made of.

Cost: $0 infrastructure. Anthropic tokens only — ArgaBench's published figure is $5.16/trial
for Opus 5 Max, so 4 tasks × 3 repeats + baseline ≈ 16 trials ≈ $80.

## 6. If devsim itself is blocked

If §1's open questions reveal the artifact contract is not reproducible in the time available:
drop the grader integration, keep devsim as a seeded local eval, and score with our own
assertions written directly from the published task semantics in `TASKS.md`. Disclose that the
rubric is ours. This is the weakest branch and should not be needed; note it so nobody invents
it under pressure at 14:00.

## 7. Disclosure block for `docs/RELIABILITY-BRIEF.md`

> **Substrate.** Arga's Free plan allows one twin per run; ArgaBench tasks provision three to
> five, so no ArgaBench task is runnable on it. We did not have multi-twin access. Every result
> below was produced by rebuilding the published scenario seed
> (`benchmark/argabench_40/scenarios/*.json`) into a local deterministic simulator and, for the
> demonstrated task, into real Slack / Stripe test mode / HubSpot / Gmail accounts. Outcomes
> were graded by ArgaBench's own `argabench_fair` verifier, unmodified, via
> `report_argabench_semantic_matrix.py`. The agent code is identical across substrates; only
> the tool executor differs. We make no claim about the official leaderboard, and we have not
> run on Arga twins.
