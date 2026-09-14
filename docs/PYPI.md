<p align="center">
  <img src="https://raw.githubusercontent.com/rajkaria/benchpress/main/docs/img/banner.svg" alt="Benchpress: the reliability layer for AI agents with write access" width="100%">
</p>

# benchpress-agent

**The reliability layer for AI agents with write access.** Benchpress is a task-agnostic control
loop you wrap around any tool layer. It reads the workspace's rules before deciding what "done"
means. It picks the right record among look-alikes and locks the rest in a deny-list **enforced in
code**. It plans only the writes the definition of done needs, **gates** every one, and **reads
every write back**. Status comes from provider state, never from an HTTP 200. Every run ends with an
auditable **receipt**.

```bash
pip install benchpress-agent        # or: uv add benchpress-agent
```

Python 3.11+. Runtime dependencies: `httpx`, `pydantic`.

**See it work in one command, no keys needed:**

```bash
uvx --from benchpress-agent benchpress demo      # or: pip install benchpress-agent && benchpress demo
```

The demo runs the real loop, gate and read-back on an in-memory workspace with a scripted model. It
plans a write to a look-alike prospect on purpose, so you can watch the gate refuse it, and it
writes `benchpress-demo/receipt.json` and a self-contained `receipt.html`.

## Quickstart

```python
import asyncio
import benchpress

async def execute_tool(tool_name: str, tool_input: dict) -> dict:
    # tool_name == "provider_api"
    # tool_input == {"provider": "stripe", "method": "POST", "path": "/v1/customers/cus_123",
    #                "query": {...}, "body": {...}}
    # Call your real API (or a sandbox/twin) and return:
    return {"ok": True, "status_code": 200, "body": {...}}

agent = benchpress.wrap("deepseek-v4-pro", execute_tool, providers=["hubspot", "stripe", "slack", "gmail"])

result = asyncio.run(agent.run(
    "Acme asked for renewal notices to go to ap@acme.example. Update billing and the CRM, "
    "and keep the account owner in the loop. Do not send external mail.",
    trace_dir="runs/acme",            # writes receipt.json
))
print(result.status)                  # "completed" | "partial" | "escalated", from read-back evidence only
print(result.context.refusals)        # every write the gate refused, with the rule that refused it
```

- `model` is a model id (`DEEPSEEK_API_KEY` / `BENCHPRESS_API_KEY` + `BENCHPRESS_API_BASE` for any
  OpenAI-compatible endpoint, `ANTHROPIC_API_KEY` for `claude-*`), a `benchpress.ModelConfig`, or
  `None` to read `BENCHPRESS_MODEL`. The Anthropic transport is covered by offline contract tests but has not
  been exercised against the live API; the published results used `deepseek-v4-pro`.
- `executor` is an async `execute_tool(tool_name, tool_input)` function or any object exposing one.
- Built-in playbooks cover **Slack, Gmail, HubSpot, Stripe and GitHub**; pass `playbooks=` for your own systems.
- `transport=` swaps the model wire protocol (bring your own client, or a scripted one in tests).
- `agent.run_sync(...)` for synchronous callers.

A real-app gateway for Slack, Gmail, HubSpot and Stripe ships in `benchpress.realapp`
(`gateway_from_env(providers)`), and the CLI runs a request end to end:

```bash
benchpress run --providers hubspot,stripe --prompt "..." --trace-dir runs/demo
benchpress receipt runs/demo --html
```

**Gate one write, no controller, no model** (the primitive every adapter and the gateway compose):

```python
from benchpress import VerifiedWrite
from benchpress.context import Action, Context, ReadBack

action = Action(
    id="w1", kind="update", provider="hubspot", method="PATCH",
    path="/crm/v3/objects/companies/701",
    body={"properties": {"email": "ap@rivermill.example"}}, fields=("email",),
    readback=ReadBack(path="/crm/v3/objects/companies/701", field_path="email"),
)
outcome = await VerifiedWrite(
    my_gateway.execute_tool,
    context=Context(user_prompt="Rivermill asked for renewal notices to go to ap@rivermill.example."),
).run(action)
print(outcome.status)            # refused | failed | unverified | verified | mismatch
print(outcome.verdict.rule)      # why the gate said yes or no
print(outcome.evidence)          # what the provider showed after the write
```

Python 3.11+. Roadmap: gateway, console, adapters for every major framework, Helm chart. See docs/ROADMAP.md in the repository.

## MCP: put the gate in front of any MCP server

```bash
pip install "benchpress-agent[mcp]"
benchpress mcp-guard --policy guard.json -- npx -y @your/mcp-server
```

`mcp-guard` is a stdio MCP proxy. Point Claude Desktop, Cursor or any MCP client at it instead of the
upstream server. Reads pass through. Writes are refused **in code** unless a policy rule allows them
(tool-name rules, argument patterns, call caps), and destructive or unannotated tools are denied by
default. Every call, allowed or refused, appends a JSONL receipt line with the tool, an argument
digest, the decision and the rule. `benchpress.shims.mcp.mcp_executor(...)` goes the other way: it
turns MCP client sessions into an executor, so the full loop can drive MCP tools. Policy format,
client config and limits: [docs/MCP.md](https://github.com/rajkaria/benchpress/blob/main/docs/MCP.md).

## OpenAI Agents SDK: guard your function tools

```python
from agents import Agent, Runner
from benchpress.shims.openai_agents import guard_tools     # pip install "benchpress-agent[openai-agents]"

policy = {"rules": [{"tool": "update_invoice", "arguments": {"status": "sent|paid"}, "max_calls": 10}]}
tools = guard_tools([get_invoice, update_invoice, delete_invoice], policy, receipts="receipts.jsonl")
result = Runner.run_sync(Agent(name="billing-ops", instructions="...", tools=tools), "Mark INV-7 as paid")
```

Reads run; `update_invoice` runs only with an allowed status and at most 10 times; `delete_invoice` never runs, and
the model is told which rule refused it. The check sits in each tool's invoker, so it holds for every run mode.
[docs/OPENAI-AGENTS.md](https://github.com/rajkaria/benchpress/blob/main/docs/OPENAI-AGENTS.md)

## Composio: guard hundreds of SaaS tools

```python
from composio import Composio
from benchpress.shims.composio import guard_composio      # pip install "benchpress-agent[composio]"

composio = guard_composio(Composio(), "guard.json", receipts="receipts.jsonl")   # same policy file as mcp-guard
```

Every `tools.execute` (and the provider execute hook agents use) is classified and checked against the policy before
Composio runs it; refusals never execute, and every call leaves a receipt. `composio_executor(client, user_id=...)`
lets `benchpress.wrap(...)` drive Composio tools through the full loop.
[docs/COMPOSIO.md](https://github.com/rajkaria/benchpress/blob/main/docs/COMPOSIO.md)

## TypeScript / Vercel AI SDK

`npm install benchpress-guard`: `guardTools(tools, policy)` wraps AI SDK tools with the same policy file, classifier,
refusal text and receipt lines as the Python guards (80 cross-language parity cases).
[npm](https://www.npmjs.com/package/benchpress-guard)

## Policy packs and the public gate-rule corpus

```bash
benchpress policy list                 # billing, customer-success, it-offboarding, release-engineering
benchpress policy show billing
benchpress gate check                  # 178 bundled cases: what the gate allows and refuses, and why
benchpress gate check my-cases/        # add your own YAML cases
```

Policy packs are YAML rule sets per business function (draft-only outbound mail, no refunds or deletes on
money objects without an approval fact, no CRM deletes, no force-push). The gate enforces them **in code**
and names the rule id in every refusal:
`benchpress.wrap(model, executor, providers=[...], policy_packs=benchpress.load_policy_packs(["billing"]))`. From the CLI:
`benchpress run --providers stripe,gmail --policy-pack billing --prompt "..."`.
Packs only ever add refusals; with no pack the gate behaves exactly as before. The corpus documents the gate's
real behavior; the five gaps it originally recorded as strict xfails were fixed in 0.3.2 and 0.3.3.
[Packs](https://github.com/rajkaria/benchpress/blob/main/docs/POLICY-PACKS.md) ·
[corpus format](https://github.com/rajkaria/benchpress/blob/main/docs/GATE-CORPUS.md)

## Rehearse: the model never decides a production write live

```python
from benchpress.rehearse import rehearse, replay

rehearsal = await rehearse(request, providers, stage_factory, n=3)   # n runs, each on a fresh copy of state
if rehearsal.converged:                                              # same normalized final state, same writes, zero refusals
    receipt = await replay(rehearsal, production.execute_tool)       # no model: gate + id substitution + read-back per write
```

A rehearsal converges only when every run ends in the same normalized state hash, with identical writes and zero gate
refusals; otherwise you get a divergence report naming the first differing state path and write. Replay refuses a
non-converged rehearsal and stops at the first refusal or read-back mismatch. You supply the stage factory (sandbox,
twin, or seeded copy); forking live production state is not automated yet.
[docs/REHEARSE.md](https://github.com/rajkaria/benchpress/blob/main/docs/REHEARSE.md)

## Closed loop: regressions and audit export

```bash
benchpress regress runs/acme/receipt.json --out gate-cases/   # this run's gate decisions become permanent test cases
benchpress gate check gate-cases/                             # fails the day the gate would decide differently
benchpress receipts export runs/ --format csv --out audit.csv # who changed which record, why, with what evidence
```

## The loop

`P0 orient → P1 policy sweep → P2 resolve (lock look-alikes) → P3 definition of done → P4 plan →
P5 execute through the gate + read-back → P6 verify (+ one repair round) → P7 deliver`

| Principle | What it means in code |
|---|---|
| **Code beats prompt for safety** | The gate refuses writes to protected ids, prohibited verbs, sent (vs drafted) external mail, control-plane paths. The prompt only explains. |
| **State is truth** | Every write is read back; status is computed from evidence, never from a 2xx. |
| **Task-agnostic** | No code keyed to task ids, seeded names or domains. CI greps for them. |
| **Provider content is data** | Policies found in the workspace are classified; injection attempts are flagged, not obeyed. |
| **Honest endings** | P7 always runs, with whatever evidence exists. Timeouts and budget exhaustion end in a receipt, not a crash. |

## Evidence

Benchpress was built for the Multi-App AI Agent Hackathon (2026-09-13) and is measured against
ArgaBench's hardest published scenario, graded by ArgaBench's verifier on the published seed rebuilt
locally and on real Slack, Gmail, HubSpot and Stripe (test mode). It makes no claim about the
official leaderboard. On local twins of the published seed under ArgaBench's **unmodified runner and grader**,
Benchpress passed 3/3 and a same-model baseline 0/3. On real Slack/Gmail/HubSpot/Stripe (test mode), scored by a
line-cited port of the criteria, both arms failed 0/2 on the published seed: Benchpress missed one assertion (real
HubSpot rejects the seed's reserved `.example` address), the baseline three, and neither was unsafe. On a seed
adaptation with routable addresses, one Benchpress trial passed 13/13 (no baseline was run there). These trials ran
on the build-day gate, before the 0.3.2 gate fixes. Every trial: [reports/summary.md](https://github.com/rajkaria/benchpress/blob/main/reports/summary.md).
Methods, results, failures and cost are in the repository:
[README](https://github.com/rajkaria/benchpress#readme) ·
[reports](https://github.com/rajkaria/benchpress/tree/main/reports) ·
[disclosure](https://github.com/rajkaria/benchpress#17-disclosure).

## Status

Alpha (`0.x`). The public API is `benchpress.wrap`, `Benchpress.run`, `run_trial`, `TrialResult`,
`ModelConfig`, `Ablations`, `Gate`. MCP, OpenAI Agents SDK and Composio support ship as the `[mcp]`, `[openai-agents]` and `[composio]` extras; policy packs, the gate-rule corpus and Rehearse ship in the core package.
New capabilities land in minor releases; see the [roadmap](https://github.com/rajkaria/benchpress#16-what-benchpress-becomes).

Apache-2.0 · Built by [Raj Karia](https://github.com/rajkaria)
