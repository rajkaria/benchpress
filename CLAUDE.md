# Benchpress — Project Instructions

Hackathon entry for the **Multi-App AI Agent Hackathon, Sunday 2026-09-13 09:30–16:00 PT**
(judges: Arga Labs founders; $10k first). Benchpress is a task-agnostic agent scaffold
shipped as a candidate adapter inside the judges' public benchmark, ArgaBench.

## Read first, in this order
1. [`docs/BUILD-SPEC.md`](./docs/BUILD-SPEC.md) — the complete product spec (architecture,
   phases, gate rules, harness integration, eval plan, demo script, vision).
2. [`docs/SUNDAY-PLAN.md`](./docs/SUNDAY-PLAN.md) — hour-by-hour plan with cut lines.
3. [`docs/RESEARCH.md`](./docs/RESEARCH.md) — every verified fact: judges, criteria,
   ArgaBench harness internals, grader assertions, the 40 tasks, pricing. Do not re-research.
4. [`docs/FOUNDERS-EMAIL.md`](./docs/FOUNDERS-EMAIL.md) — access request + fallback.

## Layout
- `arga-twins-benchmark/` — vendored clone of ArgaLabs/arga-twins-benchmark (gitignored
  here; becomes the fork `rajkaria/arga-twins-benchmark`, branch `benchpress`). Our code
  lands as `src/arga_twins_benchmark/agents/benchpress.py` + `src/benchpress/` + two
  profiles in `benchmark/argabench_40/model_matrix.json`. **Never edit graders, gateway,
  snapshot capture or seeds.**
- `src/benchpress/` — the package (adapter, model, tools, gate, phases/, playbooks/,
  verify, report, realapp, cli, lemma). Python 3.12, pyright strict, ruff (line 120).
- `tests/` — gate, DoD rules, playbooks, replay.
- `reports/` — committed semantic reports + compare tables. `runs/` gitignored.

## Hard rules
- **Task-agnostic.** No code keyed on task IDs, seeded names, emails or domains. A judge
  must find nothing that would not generalize to a 41st task. Verifier source is read by
  humans to learn semantics; it is never imported at runtime.
- **Same rules as every candidate.** Two tools (`provider_api`, `provider_docs`), 160/40
  calls, 1,800 s, harness system prompt, no control-plane paths (`/admin`, `/_twin`,
  `/inspect`, `/reset`, roots, schema/health routes, GraphQL introspection).
- **Code beats prompt for safety.** `gate.py` refuses DELETE, sends, charges, merges,
  protected-record writes, out-of-scope providers, unplanned writes, field smuggling,
  external destinations, replayed fingerprints.
- **State is truth.** Every write is read back; success is reported from evidence only.
- **Disclose everything** in `docs/RELIABILITY-BRIEF.md`: real vs twin, what failed, cost.

## Gate (green before any scored run)
```bash
cd arga-twins-benchmark && uv sync --group dev && uv run pytest -q && uv run ruff check . && uv run pyright
```

## Access
Arga Free = 1 twin/run; every task needs 3–5 twins. Founders email sent Saturday; fallback
Pro $1,250/mo self-serve. Env: `ARGA_API_URL=https://api.argalabs.com`, `ARGA_API_KEY`,
`ANTHROPIC_API_KEY`, optional `LEMMA_API_KEY`/`LEMMA_PROJECT_ID`.

## Memory
Cross-session facts live in the Hunch memory dir
(`~/.claude/projects/-Users-rajkaria-Projects-hunch/memory/project_multiapp_agent_hackathon.md`).
Keep this CLAUDE.md updated with state + next steps at the end of every session.

## State (update every session)
- 2026-09-12: docs written (spec, plan, research, email). Benchmark cloned at HEAD
  `4a81785`. No code yet. Next: Saturday prep checklist in SUNDAY-PLAN.md, then Sunday.
