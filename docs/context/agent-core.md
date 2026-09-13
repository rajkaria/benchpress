---
feature: agent-core
globs:
  - src/benchpress/**
  - tests/test_controller.py
  - tests/test_gate.py
  - tests/test_model.py
  - tests/test_prompts.py
  - tests/test_playbooks.py
  - tests/test_realapp_gateway.py
updated: 2026-09-13
---

# Agent core — the Benchpress loop (`src/benchpress`)

## Current state — what's working, deployed, broken
- Working, all tests green: `model.py` (DeepSeek/OpenAI-compatible + Anthropic transports; `emit` forced
  tool call with JSON-mode fallback; `explore` bounded loop; metering), `controller.py` (P0–P7 with
  timeouts, ablations `no_policy_sweep|no_gate|no_readback`, receipt.json), `phases/*`, `gate.py`
  (decodes base64url `raw`, exact identity match on protected ids), `tools.py` (harness-shaped
  `tool_call` events with verbatim outputs), `playbooks/{slack,gmail,hubspot,stripe}.py`,
  `realapp.py` (harness-faithful gateway, `gateway_from_env`, devsim via `DEVSIM_<P>_URL`),
  `adapter.py` (ArgaBench `invoke_model` contract), `report.py`, `cli.py`.
- Verified live: DeepSeek forced tool calls; Stripe and Slack playbooks through the real gateway.
- WIP (pyright relaxed to basic, no tests): `receipt_html.py` (renders; needs tests, `--html` CLI flag,
  sample page). Anthropic transport untested (no key).
- Not yet run end to end on real apps with the model (needs Gmail refresh token + HubSpot seeding).

## Recent changes — files touched and why
- Whole package written 2026-09-13 (see git log). Notable fixes: escalation posts only when a task fact
  can be cited (the grader marks fact-less Slack posts `unsafe: irrelevant_additive_write`);
  `ModelConfig.from_env` loads `.env` when keys are absent; prohibition parsing skips descriptive
  "never been" clauses; plan dry-run uses `gate.check` so the `no_gate` ablation is real.

## Key decisions — choices and trade-offs
- Model-agnostic client over any chat-completions endpoint (DeepSeek `deepseek-v4-pro` today; Anthropic
  later) instead of the Anthropic SDK: cheap iteration, same code path for candidate and baseline.
- Model produces *intents*; playbooks build exact requests (encoding, read-backs); code owns every
  safety decision (gate) and always-on DoD rules (channel update, structured result, review policy ⇒
  unsent draft + owner review record).
- Deliverables (draft, review record, update) are controller-owned, never planned by the model.
- Channel messages never name protected records (our gate would refuse them; the receipt has details).

## Next steps — specific, actionable
1. First real-app run: `uv run benchpress run --prompt-file <ECOM-02 prompt> --providers slack,gmail,hubspot,stripe`
   after seeding all four apps; read `runs/local/*/receipt.json`; fix phase prompts as needed.
2. Finish `receipt_html.py` (tests, `benchpress receipt --html`, `docs/img/receipt-sample.html`).
3. Restore strict pyright on WIP files.
