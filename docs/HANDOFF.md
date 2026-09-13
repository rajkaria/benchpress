# Handoff — state at the end of build day 2 (2026-09-14, ~02:10 IST / 13:40 PT)

Read this, then `CLAUDE.md`, then `reports/INDEX.md`.

## Where things are

- Repo: **github.com/rajkaria/benchpress** (public), `main` = worktree branch
  `claude/benchpress-hackathon-day2-a8d916` (`git push origin HEAD:main`). Vendored harness symlink
  `arga-twins-benchmark` → `/Users/rajkaria/Projects/benchpress/arga-twins-benchmark` (commit `4a81785`).
- Keys in the worktree's gitignored `.env`: DEEPSEEK (model), STRIPE test, SLACK bot, HUBSPOT private app
  (fresh portal, needs `crm.schemas.*.write` for custom properties), GMAIL client + **refresh token (working)**,
  `BENCHPRESS_MAX_TOKENS=16000`. Rotate all after the event.
- Gate: `uv run pytest -q && uv run ruff check . && uv run pyright` — green (591 tests).

## What is built (all merged)

Everything in `docs/AGENT-TASKS.md` landed: assertions + contract (T2.6), run loop / baseline / compare /
gate replay (T2.7), Stripe and HubSpot twins (D5/D4), receipt HTML (T3.6), `devsim/baseline_candidate.py`,
`evals/rescore.py`, `scripts/summarize_reports.py`, `devsim serve --empty`. Plus a public `benchpress-agent`
package API from a parallel session.

## How to run (the only sequences that produce clean data)

```bash
# twins, unseeded; evals.run/contract seed them. ONE evals.run at a time per substrate.
uv run python -m devsim serve --task ECOM-02 --empty --print-env      # copy the exports into the shell
uv run python -m evals.contract --substrate devsim                     # oracle=pass unsafe=unsafe fail=fail
uv run python -m evals.run --scenario billing-review --agent benchpress --substrate devsim --out runs/devsim
# real apps: run in a shell WITHOUT DEVSIM_* exported
uv run python -m evals.seed --task ECOM-02 --apps slack,stripe,hubspot,gmail --verify
uv run python -m evals.run --scenario billing-review --agent benchpress --substrate real --apps slack,stripe,hubspot,gmail --out runs/real
# unmodified ArgaBench runner + graders (spins its own twins)
uv run python -m devsim run --task ECOM-02 --profile benchpress-deepseek-v4-pro --candidate module:devsim.benchpress_candidate:benchpress --output runs/devsim-harness/benchpress --repeat 3
uv run python -m devsim report runs/devsim-harness/benchpress/repeat-01 reports/devsim/benchpress/repeat-01 --profile benchpress-deepseek-v4-pro
uv run python scripts/summarize_reports.py && uv run python -m evals.compare runs --out reports
```

## Lessons that cost time today

- Two trials on one substrate at once = duplicate seeds = the agent (correctly) escalates. Kill → reset with
  the trial's `seed-manifest.json` → verify → rerun.
- DeepSeek returns empty content when reasoning eats `max_tokens`; `emit` now retries with a doubled budget.
- Real HubSpot rejects `.example` e-mails (INVALID_EMAIL) and companies have no `email` property; the seeder
  copes, the agent's HubSpot e-mail write cannot succeed on real HubSpot. Disclosed in the brief.
- Three Slack messages from 2026-09-13 (`ts 1789319401…`) cannot be deleted by the bot; harmless residue.
- Stale `scripts/gmail_oauth.py` processes hold port 8765; kill before re-running.

## Not done

Ablations, DEV-03/CRM-02, video, PDF export of the brief, tag `v0.1.0-hackathon`.
