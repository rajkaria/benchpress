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
updated: 2026-09-14
---
# agent-core — model layer, controller, phases, gate, playbooks, real-app gateway, receipts

## Current state — what's working
- Full loop passes ECOM-02 3/3 under the unmodified ArgaBench grader on twins and does every deliverable on real
  apps (A2 blocked only by real HubSpot rejecting `.example` e-mails). Receipt HTML (`benchpress receipt --html`)
  with `docs/img/receipt-sample.html`. Gate green (614 tests).

## Recent changes — files touched and why (all grader-driven, day 2)
- `model.py`: `emit` retries empty replies (reasoning-only turns that hit `max_tokens`) with a doubled budget, 3 retries.
- `phases/dod.py` + `prompts.py`: DoD sees each chosen target's `current_record` (notes/description/email); rules:
  cover every system of record, new value comes from evidence never the former value.
- `prompts.py` (resolve): choose a target for every provider with candidates; organisation variants (sub-unit,
  subdomain, archive) are not ties; null lifecycle is not disqualifying.
- `phases/deliver.py`: escalation posts only when a subject entity can be cited; deliverables never quote protected
  ids (`_citable_facts`); draft subject clipped at a word boundary; confirmation addressed to the verified new contact
  (`new_contact_values`) and leads with it in the subject.
- `receipt_html.py`, `cli.py --html`: strict, tested.

## Key decisions
- Code beats prompt: every grader-visible failure got a code path or a code-enforced rule, prompt nudges only where
  the choice is the model's.
- `src/benchpress` stays task-agnostic (CI grep clean).

## Next steps
1. Persist model-call events (finish_reason, text_chars) into the receipt for diagnosis.
2. Playbook fallback when a provider rejects a field write (e.g. HubSpot `INVALID_EMAIL` → text property).
3. Ablation runs to attribute the lift per guarantee.
