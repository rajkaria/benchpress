# Sunday plan — 2026-09-13 (all times Pacific)

Build window is 09:30–16:00. Judging 16:00–16:40. Every block has a cut line. The rule at
each cut: ship what is green, disclose what is not.

## Saturday 2026-09-12 (prep — no code, per the spirit of the build window)

- [ ] Submit ETHOnline (deadline 09:00 PT Sunday). Do not let it collide with the opening.
- [ ] Send `docs/FOUNDERS-EMAIL.md` to founders@argalabs.com (cc phillip via the cal.com
      profile email if visible). Ask for a reply by 08:30 PT Sunday.
- [ ] Register on the Google Form. "What will you build?": *"Benchpress — a task-agnostic
      agent scaffold shipped as a candidate adapter inside ArgaBench, targeting the tasks no
      frontier model has passed."* Apps: Slack, Gmail, HubSpot, Stripe, GitHub, Linear.
- [ ] Accounts: Arga (login.argalabs.com/get-started, `arga login`, `arga whoami`),
      Anthropic key with ≥$400 headroom, Lemma key (optional), Stripe test mode, HubSpot free
      portal, scratch Slack workspace + bot token, scratch Gmail + OAuth token (real-app mode).
- [ ] `cd ~/Projects/benchpress/arga-twins-benchmark && uv sync --group dev && uv run
      pytest -q` green. `uv run python scripts/build_argabench_40.py validate` green.
- [ ] Fork ArgaLabs/arga-twins-benchmark to rajkaria; add remote; branch `benchpress`.
- [ ] Read once, end to end: `scripts/run_argabench_40.py`, `agents/anthropic.py`,
      `providers/gateway.py`, `reporting/argabench_fair.py`. Note exact CLI flags in
      `docs/RESEARCH.md` if they differ from the spec.
- [ ] Sleep.

## Sunday

| Time | Block | Output | Cut line |
|---|---|---|---|
| 08:00–08:30 | Access check | `arga whoami` on a plan that allows multi-twin runs. If no founder reply: buy Pro. | Hard gate. Nothing else matters without this. |
| 08:30–09:00 | Stage + seed-check | `build_argabench_40.py stage`, `seed-check --task ECOM-02 --task DEV-03`. Record twin provision time. | If provision >5 min, set `--concurrency 2` for the day. |
| 09:00–09:30 | Opening | Attend. Ask organizers in chat whether hackathon Arga credits exist. | — |
| 09:30–10:15 | Skeleton | `BenchpressAdapter` returning a stub result; runner branch; model matrix profiles; `run_argabench_40.py --profile benchpress-opus-5-high --task ECOM-02` completes end to end (fails grading, that's fine). Commit. | Must be done by 10:15. |
| 10:15–11:15 | Tool bus + gate + playbooks (slack, gmail, hubspot, stripe, github, linear) | Gate tests green (≥25 cases). Playbook ops for read/search/create-draft/post-message/patch. | Drop salesforce/jira/notion playbooks (not in tier A/B twins except CRM-02/CRM-05 which have salesforce: keep a minimal salesforce read/patch). |
| 11:15–12:30 | Phases P0–P4 | Orient, policy sweep, enumerate/resolve, DoD, plan with forced-JSON outputs. Dry run on ECOM-02 twin: prints DoD + plan; no writes yet. | If P1 finds the policy email and P2 locks the prospect by 12:30, proceed. |
| 12:30–13:30 | Phases P5–P7 | Execute through gate, read-back, deliverables, final JSON. First full ECOM-02 trial graded by the harness. Fix until PASS. | 13:30 is the scored-run start. If not passing, run anyway and fix in parallel. |
| 13:30–14:45 | Scored runs | Tier A (ECOM-02, CRM-02, CRM-05) + DEV-03 × 3 repeats, `--concurrency 4`; baseline `opus-5-high` × 1 on the same tasks in parallel. Grade with the report scripts as runs finish. | Tier C only if all A+B trials are launched by 14:15. |
| 14:45–15:15 | Brief + README | `RELIABILITY-BRIEF.md` from the semantic reports; README results table; VISION.md (paste from spec §23). Commit + push. | — |
| 15:15–15:45 | Video | Record per `docs/BUILD-SPEC.md` §21. Screen: leaderboard → real-app or twin run → receipt JSON → grader PASS lines → DEV-03 gate refusal → results table. Upload unlisted. | If real-app mode is not wired, use the twin run for the "agent" segment; say "Arga twins, same APIs" on screen. |
| 15:45–16:00 | Submit | Form: repo, video, brief. Final `git push`. Screenshot the submission. | Hard stop 15:55. |
| 16:00–16:40 | Judging | Be present; have the receipt and grader JSON open; Q&A prep from spec §22. | — |

## Parallelism (if a teammate joins)

- Person A: tool bus, gate, playbooks, tests.
- Person B: phases and prompts, harness integration.
- Person C (optional): video assets, README, brief template, real-app credentials.

## Scored-run commands (fill in from vendored HEAD)

```bash
export ARGA_API_URL=https://api.argalabs.com ARGA_API_KEY=… ANTHROPIC_API_KEY=…
for r in 1 2 3; do
  uv run python scripts/run_argabench_40.py --output runs/bp-r$r --profile benchpress-opus-5-high \
    --task ECOM-02 --task CRM-02 --task CRM-05 --task DEV-03 --concurrency 4 &
done
uv run python scripts/run_argabench_40.py --output runs/base-r1 --profile opus-5-high \
  --task ECOM-02 --task CRM-02 --task CRM-05 --task DEV-03 --concurrency 4
wait
for r in 1 2 3; do uv run python scripts/report_argabench_semantic_matrix.py runs/bp-r$r reports/bp-r$r; done
uv run python scripts/report_argabench_semantic_matrix.py runs/base-r1 reports/base-r1
uv run python scripts/report_argabench_repeats.py --repeat 1=reports/bp-r1/semantic-report.json \
  --repeat 2=reports/bp-r2/semantic-report.json --repeat 3=reports/bp-r3/semantic-report.json --output reports/bp-repeated
```

(`--task` may be single-valued in the runner; if so, loop per task. Check `add_argument`
at line ~1318 of `scripts/run_argabench_40.py`.)

## Failure playbook

| Symptom | Action |
|---|---|
| Twin provision timeouts | Lower concurrency; `arga runs logs --errors-only`; rerun infra-invalid trials (harness excludes them from scoring). |
| Grader says `reviewed_unsent_confirmation` fail | Check draft text has entity + both contacts; check Slack review message has "review" + "owner" + entity. Fix P7 templates, rerun that task only. |
| Grader says `unsafe: protected or distractor resource changed` | A read-back or enumeration op mutated? Check ledger for non-GET on protected ids; tighten gate. |
| `structured_result` fail | Final JSON missing a fact; ensure all identifiers from DoD are in `facts`. |
| Over-refusal on CRM-02 (two Alder accounts) | Resolution prompt: the reply "belongs to one of two accounts" — pick by domain/email evidence in Gmail thread; only escalate on a true tie. |
| Model tool-limit exceeded | Phase caps too generous; lower P1/P2 caps by 10 each. |
