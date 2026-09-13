# Benchpress in front of OpenAI Agents SDK tools

`benchpress.shims.openai_agents.guard_tools` puts code-enforced authority between an OpenAI Agents SDK
agent and its function tools. Every call is classified (read / write / destructive) and checked
against a policy **before the tool body runs**. A refused call never executes. The model gets a clear
error string and can adjust. Every call, allowed or refused, leaves a JSONL receipt.

Verified against `openai-agents` 0.22.2.

## Install

```bash
pip install "benchpress-agent[openai-agents]"   # pulls openai-agents>=0.22.2; plain benchpress-agent never imports it
```

## Example

```python
from agents import Agent, Runner, function_tool
from benchpress.shims.openai_agents import guard_tools

@function_tool
def get_invoice(invoice_id: str) -> str: ...
@function_tool
def update_invoice(invoice_id: str, status: str) -> str: ...
@function_tool
def delete_invoice(invoice_id: str) -> str: ...

policy = {"rules": [{"tool": "update_invoice", "arguments": {"status": "sent|paid"}, "max_calls": 10}]}
tools = guard_tools([get_invoice, update_invoice, delete_invoice], policy, receipts="receipts.jsonl")
agent = Agent(name="billing-ops", instructions="Keep invoices current.", tools=tools)
result = Runner.run_sync(agent, "Mark invoice INV-7 as paid")
```

With this policy `get_invoice` runs (reads are allowed), `update_invoice` runs only with `status`
`sent` or `paid` and at most 10 times, and `delete_invoice` never runs. The model sees
`benchpress refused 'delete_invoice' [no_allow_rule]: 'delete_invoice' is classified destructive and no policy rule allows it`.

`guard_tools` returns **copies**. Pass the copies to the agent; the originals stay unguarded.

## How it enforces

The guard replaces each copy's `on_invoke_tool`, the SDK's single entry point for running a function
tool, with a guarded invoker that decides first and only then awaits the original invoker. Because
the check sits in the invoker rather than in an optional guardrail list, it holds for `Runner.run`,
`run_sync`, streamed runs, agents-as-tools and direct `on_invoke_tool` calls. Everything else on the
tool (JSON schema, `needs_approval`, `tool_input_guardrails` / `tool_output_guardrails`, timeouts,
`failure_error_function`) is carried over unchanged.

A refusal is returned as a string, the SDK's convention for a tool error the run survives
(`on_invoke_tool` may "return a string error message (which will be sent back to the LLM)").

## Classification

In order:

1. `classes={"sync_all": "write"}` passed to `guard_tools`;
2. the policy file's `classes`;
3. the name heuristic `classify_tool_name`. The name is split on `_`, `-` and camelCase:
   - any destructive verb anywhere is **destructive**: `cancel chargeback delete destroy drop erase kill purge refund remove revoke terminate truncate void wipe`;
   - otherwise a leading read verb is **read**: `check count describe download export fetch find get inspect list load lookup peek preview query read retrieve search show summarize view`;
   - anything else is **write**.

The heuristic errs toward the stricter class when a destructive verb appears anywhere
(`get_and_remove_item` is destructive). It only sees the name. A tool named `get_report` that also
emails the report is a read to the heuristic, so declare such tools in `classes`.

## Policy

The policy format is the one `benchpress mcp-guard` uses, from the shared module
`benchpress.shims.guard_policy`, so one JSON file can govern both. See
[docs/MCP.md, Policy file](./MCP.md#policy-file) for the full format. In short:

```json
{
  "reads": "allow",
  "classes": { "sync_everything": "write" },
  "rules": [
    { "tool": "admin_*", "effect": "deny", "reason": "admin tools are off limits for agents" },
    { "tool": "update_ticket", "arguments": { "status": "pending|resolved", "ticket_id": "T-\\d+" }, "max_calls": 20 },
    { "tool": "close_duplicate", "allow_destructive": true, "max_calls": 5 }
  ],
  "receipts": "agent-receipts.jsonl"
}
```

`policy` may be a `GuardPolicy`, its dict form, or a path to the JSON file.

Decision order for each call:

1. Arguments that are not a JSON object: refused (`invalid_arguments`).
2. Rules **in order**, matched by tool-name glob: `deny` refuses (`deny_rule`). `allow` applies only
   if every constrained argument fully matches its regex (`re.fullmatch` on the string value, or the
   canonical JSON of a non-string). Otherwise the next rule is tried, ending in `arguments_mismatch`.
   A matching allow then enforces `max_calls` (`max_calls`) and refuses destructive tools unless the
   rule sets `allow_destructive` (`destructive_default_deny`). Even `{"tool": "*"}` never allows a destructive tool.
3. No applicable allow rule: reads follow `reads` (default `allow`, else `reads_denied`); anything else
   is refused (`no_allow_rule`).
4. Policy packs, below, for a non-read call the rules allowed.

All tools passed to one `guard_tools` call share one set of `max_calls` counters and one receipt log.
To share counters across several agents, build the guard once and wrap each agent's tools with it:

```python
from benchpress.shims.openai_agents import create_guard

guard = create_guard("guard.json")
triage_tools = [guard.wrap(t) for t in (get_ticket, update_ticket)]
billing_tools = [guard.wrap(t) for t in (get_invoice, update_invoice)]
```

## Policy packs

Benchpress policy packs (`benchpress policy list`) judge provider writes: provider, method, path and
body. When a tool call can be expressed that way, map it and the packs apply exactly as they do in
the Benchpress mutation gate:

```python
from benchpress import load_policy_packs
from benchpress.shims.openai_agents import ProviderCall, guard_tools

def refund_call(args):
    return ProviderCall("stripe", "POST", "/v1/refunds", {"charge": args["charge_id"], "amount": args["amount"]})

tools = guard_tools(
    [create_refund],
    {"rules": [{"tool": "create_refund", "allow_destructive": True, "max_calls": 3}]},
    policy_packs=load_policy_packs(["billing"]),
    actions={"create_refund": refund_call},            # tool-name glob -> mapper(arguments) -> ProviderCall | None
    approvals={"refund_approval": "granted"},          # definition-of-done facts that can lift a pack rule
)
```

A pack refusal is `[policy_pack]`, with the pack rule id in the reason and in the receipt's
`pack_rule` (`pack:<pack>.<rule>`). The `max_calls` slot the rules had granted is given back. A
mapper that returns `None` skips the packs for that call. A mapper that raises refuses the call. Tools
with no mapper are judged by the rules alone.

## Receipts

Each call appends one JSONL line. Argument values are never stored, only a digest:

```json
{"args_digest": "sha256:9f2c...", "class": "write", "decision": "allow", "latency_ms": 3.1, "policy_rule": 0,
 "reason": "allowed by policy rule 0", "rule": "allow_rule", "tool": "update_ticket", "ts": "2026-09-13T21:04:05+00:00",
 "upstream_error": false}
```

`upstream_error` is `true` when the tool body raised (the exception still propagates), `false` when
it returned, and `null` when the call was refused. With the SDK's default `failure_error_function` a
failing body returns an error string instead of raising, so the receipt shows `false`.

Location: the `receipts` argument, else the policy's `receipts` (relative to the policy file when the
policy is a path, else the working directory), else `openai-agents-guard-receipts.jsonl` there.
`receipts=False` writes no file. Lines are always kept in memory on the guard
(`create_guard(...).receipts`).

## Testing without a model

The SDK ships a scripted model, so a guarded agent can be tested with no network:

```python
from agents import Agent, RunConfig, Runner
from agents.testing import ScriptedModel
from agents.testing.model import assistant_message, function_call

model = ScriptedModel([[function_call("delete_invoice", {"invoice_id": "INV-1"}, call_id="c1")],
                       [assistant_message("done")]])
agent = Agent(name="ops", model=model, tools=tools)
result = await Runner.run(agent, "clean up", run_config=RunConfig(tracing_disabled=True))
```

`tests/test_openai_agents_guard.py` does exactly this, plus direct invocations through the SDK's
`invoke_function_tool`.

## Limits

- **No read-back.** The guard decides and records. It does not verify what an allowed write changed.
  Verification is the Benchpress loop's job (`benchpress.wrap` + an executor), not a tool wrapper's.
- **Classification is declared or read from the name, never inferred from behaviour.** Declare `classes` for
  any tool whose name understates what it does.
- **Function tools only.** Hosted tools (web search, file search, code interpreter), computer, shell
  and apply-patch tools, and hosted MCP tools run outside the Python invoker and are rejected with
  `TypeError`. Use `benchpress mcp-guard` for MCP servers.
- Argument constraints see top-level arguments only.
- `max_calls` counters live as long as the guard object (one `guard_tools` call or one `create_guard`).
- Pack judgement needs a mapper per tool. The guard cannot infer the provider request a Python function makes.
