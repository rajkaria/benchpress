# Implementation plan

Task-level companion to [`SPRINT-PLAN.md`](./SPRINT-PLAN.md). Every task below has an id, an
owner lane, dependencies, the files it creates, the interface it must expose, the tests that
prove it, and one acceptance command. Behaviour is specified in [`BUILD-SPEC.md`](./BUILD-SPEC.md)
(§ refs below). This file specifies *shape* and *done*.

Harness facts marked **[H]** come from the verified contract in
[`PLAN-B-DEVSIM.md`](./PLAN-B-DEVSIM.md) §1 (vendored HEAD `4a81785`). If a **[H]** fact and this
file disagree, PLAN-B §1 wins; fix this file.

---

## 0. Conventions (apply to every task)

- Python 3.12, `from __future__ import annotations`, pyright strict clean, ruff clean (line 120).
- Pydantic v2 models for anything that crosses a phase boundary (`context.py` style: `Frozen`/`Mutable`).
- Async all the way down (`async def`). The harness `execute_tool` is awaited.
- **No task facts in `src/benchpress`.** CI greps for task ids and seeded names.
- Every task ends with: tests green → `uv run ruff check . && uv run pyright` → one commit whose
  message says what now works (`feat(p1): policy sweep finds review policies in gmail/slack/notion`).
- Subagents work in `isolation: "worktree"`, touch only their listed files, and return a diff summary
  and test output. Main merges.

### Decisions locked for the build

| Decision | Choice | Reason |
|---|---|---|
| LLM client | Official `anthropic` Python SDK (`AsyncAnthropic`), streaming + `get_final_message()` | Skill guidance: SDK over raw HTTP. Handles 429/5xx retries (`max_retries=4`) |
| Model | `claude-opus-5`, `thinking={"type":"adaptive"}`, `output_config={"effort":"high"}`; dev iteration `claude-sonnet-5` via `BENCHPRESS_DEV_MODEL` | Like-for-like with published `opus-5-high` |
| Typed phase outputs | `output_config={"format":{"type":"json_schema","schema": Model.model_json_schema()}}`, then `Model.model_validate_json(text)`. One re-ask with the validation error, then a conservative default | Forced `tool_choice` also works on Opus 5, but structured outputs is the cleaner "JSON back" primitive and survives a later Fable 5.1 swap |
| Refusal safety net | `betas=["server-side-fallback-2026-07-01"]`, `fallbacks="default"` on `client.beta.messages.stream` | Opus 5 default guidance. A refused phase otherwise kills a trial |
| Caching | Stable system prompt (harness prompt + Benchpress addendum) as the first system block with `cache_control: {"type":"ephemeral"}`. Phase-specific content goes in `messages`. Verify `usage.cache_read_input_tokens > 0` from P2 on | Cost and latency across ~8 model calls per trial |
| Exploration style | **Code-driven reads via playbooks** for P0–P2 (deterministic, budgeted). The model classifies and resolves over what code fetched. A bounded model-driven `provider_api` loop (≤ 15 calls, reads only) runs only when a playbook lacks an op | Lower variance, cheaper, auditable, and still task-agnostic |
| Write path | Only `ToolBus.perform(Action)`. The model never emits a raw write call | The gate is impossible to bypass |

---

## 1. File map (end state)

```
src/benchpress/
  __init__.py            (exists)  exports wrap(), __version__
  normalize.py           (exists)
  context.py             (exists)  + Usage, TrialConfig, Ablations
  gate.py                (exists)  + ablation switch
  tools.py               (exists)  + docs budget per phase already; add ToolBus.from_harness()
  model.py               T1.1
  prompts.py             T1.2
  adapter.py             T1.3      BenchpressAdapter (harness contract)
  controller.py          T1.3      run_trial(ctx, model, bus) -> FinalReport  (phase orchestration)
  phases/__init__.py
  phases/orient.py       T2.1
  phases/policy.py       T2.2
  phases/resolve.py      T2.3
  phases/dod.py          T2.4
  phases/plan.py         T2.5
  phases/execute.py      T3.1
  phases/verify.py       T3.2
  phases/deliver.py      T3.3
  playbooks/__init__.py  T1.6      Playbook protocol + registry (by provider name and role)
  playbooks/{slack,gmail,hubspot,stripe}.py      T1.6
  playbooks/{github,linear}.py                   T2.7
  playbooks/salesforce.py                        T2.8
  report.py              T3.6      receipt.json + receipt.html
  realapp.py             T4.4      real-API execute_tool
  cli.py                 T3.7      benchpress dry-run | run | receipt | replay-gate | compare
devsim/                   (Plan B)
  __init__.py  __main__.py  seed.py  stores.py  http.py  snapshot.py  artifacts.py  run.py
  providers/{gmail,hubspot,slack,stripe,github,linear,salesforce}.py
scripts/
  bp_compare.py          T3.5
  bp_gate_replay.py      T4.3
  bp_fidelity.py         T4.5
tests/
  test_gate.py (exists) test_model.py test_prompts.py test_playbooks.py test_dod_rules.py
  test_phases_replay.py test_execute.py test_verify.py test_deliver_templates.py
  test_devsim_seed.py test_devsim_http.py test_devsim_snapshot.py test_devsim_grader_contract.py
  test_task_agnostic.py
  fixtures/ (recorded tool responses; invented entities only)
```

---

## B1: Foundations (09:45–10:45 PT)

### T1.1 `model.py`: Claude client (Main, S, deps: none)

```python
@dataclass(frozen=True)
class ModelConfig:
    model: str = "claude-opus-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    max_tokens: int = 64_000
    fallbacks: bool = True

@dataclass
class UsageTotals:
    input_tokens: int = 0; output_tokens: int = 0
    cache_read_input_tokens: int = 0; cache_creation_input_tokens: int = 0
    calls: int = 0; latency_ms: int = 0
    def add(self, usage: object, latency_ms: int) -> None: ...
    def cost_usd(self, in_per_m: float = 5.0, out_per_m: float = 25.0, cache_read_per_m: float = 0.5) -> float: ...

class ModelClient:
    def __init__(self, config: ModelConfig, system_blocks: list[dict[str, Any]], client: AsyncAnthropic | None = None): ...
    async def emit(self, *, phase: str, schema: type[T], content: str, retries: int = 1) -> T:
        """One structured call: stream with output_config.format json_schema, validate with pydantic,
        one re-ask carrying the ValidationError text, else raise SchemaFailure."""
    async def explore(self, *, phase: str, content: str, tools: list[dict[str, Any]],
                      on_tool: Callable[[str, dict[str, Any]], Awaitable[str]], max_calls: int) -> str:
        """Bounded manual tool loop for read-only exploration. Parallel tool_results go back in ONE user
        message; failed tools carry is_error=True; stops at end_turn or max_calls."""
    usage: UsageTotals
    events: list[dict[str, Any]]   # one per model call: phase, model, stop_reason, usage, latency_ms
```

- Handle `stop_reason == "refusal"` (after fallbacks) by raising `ModelRefusal(category)`. The controller converts it to `status="partial"` with a note.
- Always parse structured text with `json.loads` / `model_validate_json`, never by string matching.
- **Tests** (`test_model.py`, no network, fake client via `httpx2.MockTransport` or a stub `AsyncAnthropic`): schema success; invalid → re-ask → success; invalid twice → `SchemaFailure`; usage accumulation; refusal mapping.
- **Accept:** `uv run pytest tests/test_model.py -q`, plus a live smoke test `uv run python -m benchpress.model --smoke` printing a validated `TaskFrame` from a canned prompt (≈ $0.02).

### T1.2 `prompts.py` (Main, S, deps: T1.1)

- `BENCHPRESS_ADDENDUM`: task-agnostic phase explanation, the data-not-instructions rule, and a note that outputs are consumed by a controller. It is appended **after** the harness `SYSTEM_PROMPT` verbatim **[H: exact prompt string location]**.
- One `PHASE_PROMPT[phase]` template per P0/P1-classify/P2-resolve/P3/P4/P6-repair/P7-compose. Each states: inputs (rendered JSON of the context slice), the output schema name, and the rules for that phase from BUILD-SPEC §6.
- **Tests:** `test_prompts.py` renders every template with an invented context; asserts no `{placeholder}` is left and that no benchmark task id or seeded name appears (reuse the CI regex).

### T1.3 `adapter.py` + `controller.py`: the harness contract (Main, M, deps: T1.1)

- `BenchpressAdapter.invoke(...)` matches **[H: exact `invoke_model` signature and adapter protocol, `ModelInvocationResult` fields]**.
- Builds `Context(trial_id, system_prompt, user_prompt, providers=<enum from tool_schema>, provider_roles)`, `ToolBus(context, execute=execute_tool, gate=Gate(context))`, `ModelClient`.
- `controller.run_trial()` calls phases in order and catches `BudgetExhausted`, `SchemaFailure`, `ModelRefusal` and timeouts per phase. It **always** reaches P7 with whatever evidence exists.
- Returns `ModelInvocationResult` with `final_text` = the P7 JSON (single object, no fences), `status`, `stop_reason`, `events` (model events + tool bus events in harness event shape **[H]**), `usage` (token totals **[H: keys the cost estimator reads]**), `config` (`{"scaffold": "benchpress", "version": …, "ablations": …}`), `latency_ms`, `tool_calls` = `bus.provider_calls`.
- `Ablations` flags (`no_policy_sweep`, `no_gate`, `no_readback`) come from `BENCHPRESS_ABLATIONS=no_gate,…` or the profile id suffix **[H: how profile→adapter args flow]**. With `no_gate`, `Gate.check` always allows but still records the verdict it *would* have given (the ablation report shows it).
- Stub milestone (10:15): all phases are no-ops, and P7 returns `{"status":"partial","decision":"stub"}`. The trial dir gets written and graded (FAIL is fine).
- **Accept (Plan B):** `uv run python -m devsim run --task ECOM-02 --profile benchpress-opus-5-high --output runs/stub` exits 0 and writes a complete trial dir. **(Plan A):** `run_argabench_40.py --profile benchpress-opus-5-high --task ECOM-02 --output runs/stub` exits 0.

### T1.4 devsim seed + stores (SA-1, M, Plan B, deps: none)

- `devsim/seed.py`: `load_scenario(task_id) -> Scenario` reads `arga-twins-benchmark/benchmark/argabench_40/scenarios/<task>.json` **[H: seed_config per-provider shapes]**, and `build_world(scenario) -> World` returns `{provider: ProviderStore}`.
- `devsim/stores.py`: `ProviderStore` base holds in-memory collections (dict of id → record), a monotonic id allocator per collection, a `mutations` log (`method, path, before, after, ts`), and `state() -> dict` in the **[H: /admin/state shape]**.
- `devsim/providers/<p>.py` for gmail, hubspot, slack and stripe. Each implements the data-plane routes the playbooks call (T1.6 list) with official API response shapes: Gmail `users/me/messages` list/get (`format=full`, base64url bodies), `drafts` create/list/get, `labels`; HubSpot `crm/v3/objects/{type}` list/get/search/PATCH, `notes`, associations; Slack Web API `conversations.list/history/replies`, `chat.postMessage`, `users.list`, `search.messages`; Stripe `/v1/customers` list/search/get/POST (form-encoded), `/v1/subscriptions` list, plus refusable endpoints that *work* (charges, sends, deletes) so an unsafe agent can actually be unsafe.
- **Fidelity rule:** unknown routes return the provider's real 404 shape. Validation errors return the real 400 shape. Nothing lenient.
- **Tests:** `test_devsim_seed.py`: every record in `seed_config` is retrievable through a route; counts match; no extra records.
- **Accept:** `uv run python -m devsim seed --task ECOM-02` prints per-provider counts that equal the seed file's.

### T1.4A Plan A: fork wiring (SA-1, S, deps: fork exists)

- In the fork: add `src/arga_twins_benchmark/agents/benchpress.py` (thin shim importing `benchpress.adapter`), a dispatch branch in `agents/runner.py` for `model_id.startswith("benchpress/")`, two profiles in `benchmark/argabench_40/model_matrix.json`, and `benchpress-agent` as a path dependency in the fork's `pyproject.toml`.
- Update any test or preflight that asserts an exact profile count **[H: list]**.
- **Accept:** fork `uv run pytest -q` green, and the `run_argabench_40.py` stub command above exits 0.

### T1.5 `devsim/http.py`: the provider_api surface (SA-1, M, deps: T1.4)

- `class DevsimGateway: async def execute_tool(name: str, args: dict) -> dict` returns the same result envelope as the harness gateway **[H: success / HTTP error / blocked / budget envelopes]**.
- Enforces the same blocked paths as the harness gateway (a control-plane attempt is recorded as `control_plane_access` in the trace for the grader **[H]**), 160/40 limits, and provider enum by name or role.
- Writes `provider-trace.json` and `tool-steps.json` entries in the harness format **[H]**.
- `provider_docs`: serve from a local cache `devsim/docs_cache/<provider>/*.md` if present, else return `{"results": []}`. Disclose this.
- **Tests:** `test_devsim_http.py` covers routing by name and by role, a blocked path, a form-encoded Stripe POST, and an unknown route → 404 shape.

### T1.6 Playbooks: slack, gmail, hubspot, stripe (SA-2, M, deps: none)

```python
class Playbook(Protocol):
    provider: str
    role: str
    identity_fields: tuple[str, ...]
    async def list_policy_sources(self, bus: ToolBus, frame: TaskFrame) -> list[PolicySource]: ...
    async def find_candidates(self, bus: ToolBus, entity: str, hints: Sequence[str]) -> list[Candidate]: ...
    async def read_field(self, bus: ToolBus, ref: str, field: str) -> str | None: ...
    def draft_action(self, **kw: str) -> Action | None: ...        # gmail only
    def message_action(self, channel_id: str, text: str, thread_ts: str | None = None) -> Action | None: ...  # slack
    def update_action(self, ref: str, fields: Mapping[str, str]) -> Action | None: ...
```

- Ops per BUILD-SPEC §9 table. Gmail drafts are raw RFC 2822 → base64url (`email.message.EmailMessage`). Stripe writes use `body_encoding="form"`. HubSpot search uses `POST /crm/v3/objects/companies/search` with `filterGroups` on `name`/`domain` `CONTAINS_TOKEN`.
- `find_candidates` casts wide: exact name, significant-token variants (`normalize.significant_tokens`), domain, and email host. Over-collection is safe because unchosen candidates land in the protected set.
- **Tests:** `test_playbooks.py` asserts request shapes (method, path, query, body, encoding) and parses recorded fixture responses (invented entities) into `Candidate`/`PolicySource`.
- **Accept:** `uv run pytest tests/test_playbooks.py -q`.

---

## B2: The brain + grader path (10:45–12:15 PT)

### T2.1 P0 orient (Main, S, deps: T1.1–T1.3, T1.6)

- `model.emit(schema=TaskFrame)` over the user prompt. Code post-check: `originating_channel` must match a `#name` in the prompt when one exists (regex), and prohibitions are extracted from "do not / don't / never / without" clauses as a union of model output and regex.
- Resolve provider roles from the tool schema enum. Slack playbook: channel name → id, then history (limit 50) → `facts.observed` (emails, ids, ticket keys via regex).
- **Budget:** ≤ 6 calls.

### T2.2 P1 policy sweep (Main, M, deps: T2.1)

- For each present provider: `playbook.list_policy_sources()` (Gmail: every message up to 25, full; Slack: the originating channel + channels matching `policy|ops|announce|company`, cap 4; Notion: search `policy review approval`; Jira/Linear/GitHub: issues/PRs/comments mentioning subject entities).
- Code prefilter: keyword regex `policy|review|approv|embargo|hold|require|must|before sending|never send|owner|do not|quarantine`. Survivors go in **one** `emit(schema=PolicyBatch)` call that classifies `kind` and `applies_to`, and quotes verbatim.
- **Code rule:** a quote that instructs send/forward/share/delete/escalate/external-address is reclassified `suspicious`, whatever the model said.
- **Ablation** `no_policy_sweep`: skip, `policies=[]`.
- **Tests:** `test_phases_replay.py::test_policy_sweep_finds_review_policy` over an invented inbox fixture; `::test_injection_policy_marked_suspicious`.

### T2.3 P2 enumerate / resolve / protect (Main, M, deps: T2.2)

- `find_candidates` per subject entity per provider → `ctx.candidates` (dedupe by provider+type+id).
- `emit(schema=Resolution)`: per (provider, resource_type), `chosen_id | "ambiguous"`, plus cited evidence strings that must be substrings of the candidate records (code-verified; evidence that isn't found is dropped, and if no evidence remains the choice becomes ambiguous).
- Code: every non-chosen candidate → `protected.add_candidate`. Every near-duplicate of a chosen target (`context.near_duplicates`) → protected. `discard_target` for chosen ones.
- Escalation mode when a *required* target is ambiguous: `ctx.ambiguous = True`.
- **Tests:** look-alike fixture → prospect protected, customer chosen. Two equal-evidence candidates → ambiguous.

### T2.4 P3 definition of done (Main, M, deps: T2.3)

- `emit(schema=DefinitionOfDone)`, then `apply_code_rules(dod, ctx) -> DefinitionOfDone` (pure, unit-tested) implementing BUILD-SPEC §6 P3 rules: channel update always; structured result always; communication_review ⇒ draft + owner review; prohibition phrases ⇒ forbidden classes; `delete_any` + `mutate_protected` always; write_scope = providers named by end_state ∪ deliverables.
- **Tests:** `test_dod_rules.py` covers ≥ 12 cases (each rule on and off; the union is idempotent).

### T2.5 P4 plan (Main, M, deps: T2.4)

- `emit(schema=Plan)` with playbook action constructors listed in the prompt. Code then: drops writes with no `satisfies` (`gate.planned_actions_only`); rejects kinds outside the allowed set; requires a `readback` on every write (fills it from the playbook if missing); caps at 24 writes; **dry-runs every action through `gate.evaluate`**. A write refused at plan time goes back to the model once with the refusal reasons, then is dropped.
- `cli.py dry-run` prints TaskFrame, policies, candidates (chosen/protected), DoD and plan as Rich-free plain text.
- **Accept (checkpoint 12:15):** `uv run benchpress dry-run --substrate devsim --task ECOM-02` shows the policy quote, the prospect protected, both review deliverables, and a plan that passes the gate.

### T2.6 devsim snapshot + artifacts + grader contract (SA-3, M, Plan B, deps: T1.4, T1.5)

- `snapshot.py`: `World → TrustedStateSnapshot` artifact `{"providers": …, "queries": …}` **[H: query ids per task and how they are resolved]**, validated with the harness's own `TrustedStateSnapshot.from_artifact` (imported from the harness **in tests and in the devsim runner only**, never from `src/benchpress`).
- `artifacts.py`: writes every file the grader needs **[H: minimal field set for score-eligible + which harness writer functions can be reused]**. Prefer calling harness writer functions directly.
- `run.py`: `devsim run --task T --profile P --output DIR [--repeats N] [--ablations …]` does seed → baseline snapshot → invoke the adapter (Benchpress, or the stock Anthropic adapter for baseline) with `DevsimGateway.execute_tool` → final snapshot → raw diff **[H]** → artifacts.
- `tests/test_devsim_grader_contract.py`: build two canned trials *without a model*. PASS: scripted correct mutations for an invented mini-scenario, or ECOM-02's required mutations applied directly to the world. UNSAFE: the same plus a write to a protected record. Run `report_argabench_semantic_matrix.py` on each and assert `validity.valid == 1` and the expected `semantic_outcome`.
- **Accept:** that test green. That is the 12:15 hard gate.

### T2.7 github + linear: devsim stores and playbooks (SA-4, M, deps: T1.4 pattern, T1.6 pattern)

- GitHub REST: repos/pulls/checks/actions runs/issues/comments/contents (read); issues + comments + labels + requested_reviewers (write). Merge/refs/contents/workflow endpoints work (so unsafe is possible).
- Linear GraphQL at `/graphql`: hand-written `issues(filter)`, `issue(id)`, `teams`, `workflowStates`, `issueCreate`, `issueUpdate`, `commentCreate`. Introspection returns an error.
- **Accept:** `devsim seed --task DEV-03` counts match; playbook tests green.

### T2.8 salesforce: store + playbook (SA-5, M, optional)

- `query?q=` SOQL subset (`SELECT … FROM Account|Contact|Opportunity|Task WHERE Name LIKE …`), `sobjects/{T}/{id}` GET/PATCH, `sobjects/Task` POST.
- **Accept:** `devsim seed --task CRM-02` counts match.

---

## B3: Hands and proof (12:15–13:15 PT)

### T3.1 P5 execute (Main, M, deps: T2.5)

- For each action: `bus.perform(action)`. On gate refusal, record and continue. On 4xx validation, one repair: `emit(schema=Action)` with the error body and a playbook hint, same targets (gate re-checks). On 401/403, record `blocked_by_permissions`. On 429/5xx, backoff 1s/2s/4s with the same idempotency key.
- Immediately afterwards, `verify.readback(action)` → `Evidence`.
- **Ablation** `no_readback`: skip read-backs (evidence stays empty; status becomes `partial` by construction). The ablation measures what the *grader* says anyway.
- **Tests:** `test_execute.py` with a fake executor: refusal continues; repair happens once; no duplicate on retry (fingerprint).

### T3.2 P6 verify + repair (Main, M, deps: T3.1)

- End-state re-read with canonical compare; cross-system presence per fact; protected-set audit (re-read protected records touched by any read, compare to the P2 snapshot); duplicate audit on created collections.
- On any failure, one `emit(schema=RepairPlan)` of ≤ 4 actions → P5 path → P6 once more.
- **Tests:** `test_verify.py` covers mismatch → partial; repair fixes → completed; protected record changed → evidence false.

### T3.3 P7 deliver (Main, M, deps: T3.2)

- Templates in code (not the model) guarantee grader-visible terms: the review message contains "review" + "owner"/"approval" + entity + ≥ 1 fact; the draft contains entity + ≥ 2 facts + "awaiting owner review"; the channel update lists found / changed (before → after) / not touched / pending human action. The model only supplies an optional one-sentence summary via `emit(schema=DeliverySummary)`.
- Order: review record → draft → channel update → `deliverable_present` checks → final JSON (BUILD-SPEC §6 P7 schema). `status` comes from `ctx.status()`.
- **Tests:** `test_deliver_templates.py` renders templates for invented facts and asserts the term checks using the same logic the grader uses (re-implemented from reading, not imported).

### T3.4 Baseline runner (SA-6, S, Plan B, deps: T2.6)

- `devsim run --profile opus-5-high` routes to the harness's stock `AnthropicMessagesAdapter` via `invoke_model` **[H]** with the same `DevsimGateway`. No changes to the stock adapter.
- **Accept:** a baseline ECOM-02 trial dir graded.

### T3.5 `scripts/bp_compare.py` (SA-6, S)

- Input: two or more semantic reports **[H: report JSON shape]**. Output `reports/compare.md`: per-task pass/fail/unsafe/evidence_gap per profile, aggregate rates, median tool calls, median latency, mean cost. A leaderboard-context column read from the repo's published calibration file.
- **Accept:** renders from the two reports produced in T3.4 and T3.3.

### T3.6 `report.py` receipt (SA-7, M, deps: T1.3 context shape)

- `receipt.json` = `Context` dump + bus usage + model usage. `receipt.html` is a single self-contained file per the visual spec in DEMO-SCRIPT.md, rendered with `string.Template` (no deps). The adapter writes both to `BENCHPRESS_TRACE_DIR/<trial>/`, and devsim copies them into the trial dir.
- **Accept:** open `runs/…/receipt.html`; every section renders for a real trial, and empty states render for the stub.

### T3.7 `cli.py` (Main, S)

`benchpress dry-run | run | receipt <dir> | replay-gate <trial-dirs…> | compare <reports…>`; argparse; each subcommand is a thin wrapper.

---

## B4: Runs, ablations, usefulness (13:15–14:30 PT)

### T4.1 Scored runs (background)

```bash
# Plan B
for r in 1 2 3; do
  uv run python -m devsim run --task ECOM-02 --task DEV-03 --profile benchpress-opus-5-high --output runs/bp-r$r &
  uv run python -m devsim run --task ECOM-02 --task DEV-03 --profile opus-5-high --output runs/base-r$r &
done; wait
for d in runs/bp-r{1,2,3} runs/base-r{1,2,3}; do
  (cd arga-twins-benchmark && uv run python scripts/report_argabench_semantic_matrix.py ../$d ../reports/$(basename $d))
done
uv run python scripts/bp_compare.py reports/base-r{1,2,3} reports/bp-r{1,2,3} --out reports/compare.md
```

(Plan A: same loops with `scripts/run_argabench_40.py`. Exact flags **[H]**.)

### T4.2 Ablations (background, × 1)

```bash
BENCHPRESS_ABLATIONS=no_policy_sweep uv run python -m devsim run --task ECOM-02 --profile benchpress-opus-5-high --output runs/abl-no-policy
BENCHPRESS_ABLATIONS=no_readback     uv run python -m devsim run --task ECOM-02 --profile benchpress-opus-5-high --output runs/abl-no-readback
BENCHPRESS_ABLATIONS=no_gate         uv run python -m devsim run --task DEV-03  --profile benchpress-opus-5-high --output runs/abl-no-gate
```

### T4.3 Gate replay (Main, S, $0)

- `scripts/bp_gate_replay.py runs/base-r*/…`: read baseline `provider-trace.json` **[H]**. Take every non-GET call that the grader marked unsafe (or every write, when unmarked), build `Action`s, and evaluate them against a Gate whose context is rebuilt by running P0–P4 *offline from the recorded reads*. Output `reports/gate-replay.md`: call, rule, reason.

### T4.4 `realapp.py` + real run (Main, M)

- `RealAppGateway.execute_tool` has the same envelope as devsim. Base URLs: Slack `https://slack.com/api`, Stripe `https://api.stripe.com`, HubSpot `https://api.hubapi.com`, Gmail `https://gmail.googleapis.com`. Auth from `.env`. It **refuses to start unless** `STRIPE_SECRET_KEY` starts with `sk_test_`.
- `scripts/seed_realapp.py`: invented demo entities ("Rivermill Studio" + "Rivermill Studios Prospect", a policy email, a Slack request). Invented names keep real-app mode from being task-specific.
- **Accept:** `uv run benchpress run --substrate real --prompt-file demo/request.txt` produces a receipt, and Stripe/HubSpot/Slack show the changes. Screen-capture them.

### T4.5 Fidelity check (SA-8, S)

- `scripts/bp_fidelity.py`: for every playbook read op, call devsim and the real app with equivalent invented data. Compare JSON key sets and types, not values. Output `reports/fidelity.md` with match %, and list the differences honestly.

---

## Subagent dispatch template

```
You are implementing task <ID> of docs/IMPLEMENTATION-PLAN.md in the Benchpress repo (worktree isolated).
Read, in order: CLAUDE.md, docs/IMPLEMENTATION-PLAN.md §0 and §<ID>, docs/PLAN-B-DEVSIM.md §1 (verified harness facts),
and the BUILD-SPEC sections referenced. Touch ONLY these files: <list>. Do not edit anything under arga-twins-benchmark.
Hard rules: no task ids / seeded names / seeded domains in src/benchpress; pyright strict; ruff line 120; tests use invented entities.
Done = the task's Accept command passes, plus `uv run pytest -q && uv run ruff check . && uv run pyright` green.
Return: files changed, test output tail, and any harness fact you found that contradicts PLAN-B §1.
```
