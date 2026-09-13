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

Python 3.12+. Runtime dependencies: `httpx`, `pydantic`.

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
  `None` to read `BENCHPRESS_MODEL`.
- `executor` is an async `execute_tool(tool_name, tool_input)` function or any object exposing one.
- Built-in playbooks cover **Slack, Gmail, HubSpot and Stripe**; pass `playbooks=` for your own systems.
- `transport=` swaps the model wire protocol (bring your own client, or a scripted one in tests).
- `agent.run_sync(...)` for synchronous callers.

A real-app gateway for Slack, Gmail, HubSpot and Stripe ships in `benchpress.realapp`
(`gateway_from_env(providers)`), and the CLI runs a request end to end:

```bash
benchpress run --providers hubspot,stripe --prompt "..." --trace-dir runs/demo
benchpress receipt runs/demo --html
```

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
official leaderboard. Methods, results, failures and cost are in the repository:
[README](https://github.com/rajkaria/benchpress#readme) ·
[reports](https://github.com/rajkaria/benchpress/tree/main/reports) ·
[disclosure](https://github.com/rajkaria/benchpress#17-disclosure).

## Status

Alpha (`0.x`). The public API is `benchpress.wrap`, `Benchpress.run`, `run_trial`, `TrialResult`,
`ModelConfig`, `Ablations`, `Gate`. MCP support ships as the `[mcp]` extra. Expect additions (policy packs, Rehearse) in minor
releases; see the [roadmap](https://github.com/rajkaria/benchpress#16-what-benchpress-becomes).

Apache-2.0 · Built by [Raj Karia](https://github.com/rajkaria)
