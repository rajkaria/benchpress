# Benchpress: system and reliability brief

<!-- Fill in B5 (14:30–15:10 PT). ≤ 2 pages. Every number cites a file under reports/.
     Delete every HTML comment and every [bracket] before submitting. -->

**Benchpress** is a task-agnostic control loop around a frontier model (`claude-opus-5`, effort
high) for multi-app operational work. Apps used: Slack, Gmail, HubSpot, Stripe, GitHub, Linear
[, Salesforce]. Real-app mode ran on Slack, Stripe (test mode), HubSpot [, Gmail].

## 1. System

`request → P0 orient → P1 policy sweep → P2 enumerate + protected set → P3 definition of done → P4 typed plan → P5 execute through mutation gate → P6 read-back + cross-system verify (1 repair round) → P7 deliverables + evidence-only status`

| Guarantee | Enforced by | Failure class it targets (Arga taxonomy share) |
|---|---|---|
| Policies read before intent | P1, code rule: communication-review policy ⇒ draft + owner review | Missing required deliverables (19.0%) |
| Look-alikes cannot be written | P2 protected set + gate `protected` rule | Unauthorized / wrong-target writes (28.4%) |
| Forbidden action classes impossible | Gate `action_class` / `method` / `control_plane` rules | Unsafe actions (17.0% of all trials) |
| No duplicates on retry | Fingerprint ledger + gate `idempotency` rule | Duplicate / extra resources (11.2%) |
| Success only from state | P6 read-back, status computed from evidence | Incomplete outcome (54.3%), false claims (5.2%) |
| Results communicated | P7 originating-channel update is always a deliverable | Results not communicated (4.7%) |

## 2. What was run

| Item | Value |
|---|---|
| Substrate | [Plan B: real Slack workspace, Gmail account, HubSpot free portal, Stripe **test mode**, seeded each trial from `benchmark/argabench_40/scenarios/ecom-02.json` and reset after · Plan A: Arga twins] |
| Grader | [Plan B: `evals/assertions.py`, ArgaBench ECOM-02 pass/unsafe criteria ported with file:line citations, harness commit `4a81785` · Plan A: `argabench_fair` unmodified] |
| Tasks × repeats | [ECOM-02 × 3, DEV-03 × 3, CRM-02 × N] |
| Baseline | Harness stock `AnthropicMessagesAdapter`, same model/effort, same substrate, same 160/40/1,800 s limits |
| Limits | 160 `provider_api`, 40 `provider_docs`, 1,800 s, harness system prompt |
| Date / cost | 2026-09-13 · [$ total, $ per trial] (`reports/cost.json`) |

## 3. Results

| Task | Baseline pass / fail / unsafe | Benchpress pass / fail / unsafe | Published (37 configs, context only) |
|---|---|---|---|
| ECOM-02 | [ ] | [ ] | 0 / 111 pass |
| DEV-03 | [ ] | [ ] | 90 / 111 unsafe |
| CRM-02 | [ ] | [ ] | 0 / 111 pass |

Tool calls (median), latency (median), cost per trial: [ ] (`reports/compare.md`).

### Ablations (× 1 each, `reports/ablations/`)

| Variant | Task | Outcome | Reading |
|---|---|---|---|
| Full Benchpress | ECOM-02 | [ ] | — |
| − policy sweep | ECOM-02 | [ ] | [expected: fails reviewed-draft requirement] |
| − read-back | ECOM-02 | [ ] | [ ] |
| − gate | DEV-03 | [ ] | [expected: unsafe returns] |

### Gate replay (`reports/gate-replay.md`)

[N] unsafe calls recorded in baseline trials were replayed through the gate. [N] were refused, by rule: [ ].

## 4. How we know the grading is honest

1. **Assertion-contract test:** scripted trajectories with no model involved (oracle / prospect edit / no draft) grade PASS / UNSAFE / FAIL on the real apps (`reports/assertion-contract.json`).
2. **Ported criteria:** each assertion cites the ArgaBench grader line it reproduces (`evals/assertions.py`). Where we could not reproduce a check exactly, it is listed here: [ ].
3. **Same baseline code as the benchmark:** ArgaBench's stock `invoke_model` Anthropic adapter, the same system prompt and tool schema, scored by the same assertions.
4. **Task-agnostic code:** `grep -rniE "ECOM|CRM-|DEV-0|northwind|alder|checkout_tax"` over `src/` returns nothing (CI step).

## 5. What still fails (honest)

<!-- Required. List every non-pass, the grader assertion that failed, root cause, and whether it
     is a Benchpress bug, a substrate gap, or a model error. Include escalations/over-refusals. -->

- [ ]

## 6. Threat model (Arga's eight failure classes)

| Class | Handling |
|---|---|
| Sandbox escalation / control-plane access | Static path block before the gateway. Blocked prefixes are unit-tested |
| Prompt-injection compliance | Provider content is data. Policies instructing sends/forwards/shares/deletes are recorded `suspicious`, and the external-destination rule refuses unknown domains |
| Unauthorized contract/record modification | Protected set + plan membership + field-smuggling rules |
| Confidential data leakage | External-destination rule. Drive/permission shares are a forbidden class |
| Overzealous enforcement | Minimum mutation: writes must satisfy a DoD item |
| Over-refusal | Escalate only on evidence tie. Measured: [n escalations / n trials] |
| Hallucinated success | Status computed from read-back evidence only |
| Degenerate loops | Bounded attempts per action, phase budgets, one repair round |

## 7. Disclosure

**Substrate.** [Plan B:] Arga's Free plan allows one twin per run, and ArgaBench tasks provision
three to five, so we did not run on Arga twins. Every trial loaded ArgaBench's published ECOM-02
seed into real Slack (scratch workspace; seeded messages posted by our bot under the seeded
display names), Gmail (scratch account; seeded mail inserted via API), HubSpot (free portal) and
Stripe (test mode), and reset afterwards. Outcomes were scored by our port of ArgaBench's
pass/unsafe criteria, not by the official grader. We make no claim about the official leaderboard.
**Prep.** Specs and the `normalize/context/gate/tools` modules with 61 tests were written
2026-09-12 (commits [sha]). Phases, playbooks, substrate, evals and reports were built during the
build window.
**Reproduce.** `README.md` → "Reproduce", one command per table above.
