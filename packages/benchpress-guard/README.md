# benchpress-guard

Code-enforced tool policy for [Vercel AI SDK](https://ai-sdk.dev) agents. Every tool call is classified
(read / write / destructive) and checked against a policy **before `execute` runs**. A refused call never
executes: the model gets a clear refusal string and can adjust. Every call, allowed or refused, leaves a
receipt.

It is the TypeScript port of the [Benchpress](https://github.com/rajkaria/benchpress) tool guard. The policy
file, decisions, refusal wording and receipt lines are the same as `benchpress mcp-guard` and the
OpenAI Agents SDK shim (`pip install benchpress-agent`), verified by a cross-language parity test.

Zero runtime dependencies. Node 18+. Verified against `ai` 7.0.99 (works with any AI SDK whose tools have
`execute(input, options)`).

## Install

```bash
npm install benchpress-guard
```

## Example

```ts
import { generateText, tool } from "ai";
import { z } from "zod";
import { guardTools } from "benchpress-guard";

const tools = {
  getInvoice: tool({ description: "Read an invoice", inputSchema: z.object({ id: z.string() }), execute: async ({ id }) => db.get(id) }),
  updateInvoice: tool({ description: "Set status", inputSchema: z.object({ id: z.string(), status: z.string() }), execute: async (a) => db.update(a) }),
  deleteInvoice: tool({ description: "Delete an invoice", inputSchema: z.object({ id: z.string() }), execute: async ({ id }) => db.delete(id) }),
};

const policy = { rules: [{ tool: "updateInvoice", arguments: { status: "sent|paid" }, max_calls: 10 }] };

const { text } = await generateText({
  model,
  tools: guardTools(tools, policy, { receipts: "receipts.jsonl" }),
  prompt: "Mark invoice INV-7 as paid",
});
```

With this policy `getInvoice` runs (reads are allowed), `updateInvoice` runs only with `status` `sent` or
`paid` and at most 10 times, and `deleteInvoice` never runs. The model sees:

```
benchpress refused 'deleteInvoice' [no_allow_rule]: 'deleteInvoice' is classified destructive and no policy rule allows it
```

`guardTools` returns a new tools record with the same keys and the same TypeScript type. The originals stay
unguarded. Tools without `execute` (client-side tools) pass through unchanged. Works the same with
`streamText`, agents and direct `execute` calls, because the check lives inside `execute` itself.

## How it enforces

Each guarded `execute` decides synchronously, then:

- **refused**: records a receipt and returns the refusal string. The original `execute` is never called.
- **allowed**: calls the original `execute` with the same `input` and `options` and returns its result
  unchanged, whether a value, a promise, or an async iterable (preliminary tool results keep streaming).
  The receipt's `upstream_error` is `true` if it threw or rejected (the error still propagates).

A refusal is a string regardless of the tool's declared output type, so a UI that renders typed tool
outputs should expect `string` results starting with `benchpress refused`.

## Classification

In order:

1. `options.classes` passed to `guardTools` / `createGuard`, e.g. `{ syncAll: "write" }`;
2. the policy's `classes`;
3. the name heuristic `classifyToolName`. The name is split on `_`, `-` and camelCase:
   - any destructive verb anywhere is **destructive**: `cancel chargeback delete destroy drop erase kill purge refund remove revoke terminate truncate void wipe`;
   - otherwise a leading read verb is **read**: `check count describe download export fetch find get inspect list load lookup peek preview query read retrieve search show summarize view`;
   - anything else is **write**.

The heuristic only sees the name. A `getReport` tool that also emails the report is a read to it, so declare
such tools in `classes`.

## Policy

```json
{
  "reads": "allow",
  "classes": { "syncEverything": "write" },
  "rules": [
    { "tool": "admin*", "effect": "deny", "reason": "admin tools are off limits for agents" },
    { "tool": "updateTicket", "arguments": { "status": "pending|resolved", "ticketId": "T-\\d+" }, "max_calls": 20 },
    { "tool": "closeDuplicate", "allow_destructive": true, "max_calls": 5 }
  ],
  "receipts": "agent-receipts.jsonl"
}
```

`policy` may be the object, a path to the JSON file, or the result of `loadPolicy`. `loadPolicy(pathOrObject)`
validates it and throws a `PolicyError` naming the problem (`rules[1].effect must be "allow" or "deny"`).
Unknown keys are rejected.

| Key | Meaning |
|---|---|
| `rules[].tool` | glob over tool names (`*`, `?`, `[abc]`, `[!abc]`), case-sensitive, whole name |
| `rules[].effect` | `allow` (default) or `deny` |
| `rules[].arguments` | top-level argument name → regex that must **fully** match the value (a string as is, anything else as its canonical JSON) |
| `rules[].max_calls` | allows per guard; further calls are refused |
| `rules[].allow_destructive` | a destructive tool is refused under an allow rule unless this is `true` |
| `rules[].reason` | text used as the decision reason |
| `classes` | tool name → `read` / `write` / `destructive` |
| `reads` | `allow` (default) or `deny` for reads without an allow rule |
| `receipts` | JSONL receipt path, relative to the policy file (or the working directory for an object) |

Decision order for each call:

1. Input that is not an object: refused (`invalid_arguments`).
2. Rules **in order**, matched by tool-name glob. `deny` refuses (`deny_rule`). `allow` applies only if every
   constrained argument fully matches; otherwise the next rule is tried, ending in `arguments_mismatch`.
   A matching allow then enforces `max_calls` (`max_calls`) and refuses destructive tools unless the rule sets
   `allow_destructive` (`destructive_default_deny`). Even `{ "tool": "*" }` never allows a destructive tool.
3. No applicable allow rule: reads follow `reads` (`read`, or `reads_denied`); anything else is refused
   (`no_allow_rule`).

All tools passed to one `guardTools` call share one set of `max_calls` counters. To share counters across
several agents or tool sets, build the guard once:

```ts
import { createGuard } from "benchpress-guard";

const guard = createGuard("guard.json", { receipts: "receipts.jsonl" });
const triageTools = guard.wrapAll({ getTicket, updateTicket });
const billingTools = guard.wrapAll({ getInvoice, updateInvoice });
guard.receipts; // every receipt so far, in memory
```

## Receipts

Each call produces one receipt. Argument values are never stored, only a digest:

```json
{"args_digest": "sha256:9f2c...", "class": "write", "decision": "allow", "latency_ms": 3.1, "policy_rule": 0, "reason": "allowed by policy rule 0", "rule": "allow_rule", "tool": "updateTicket", "ts": "2026-09-13T21:04:05.123000+00:00", "upstream_error": false}
```

- `receipts: "path.jsonl"` appends JSONL lines (directories are created), serialized exactly as Python's
  `json.dumps(line, ensure_ascii=False, sort_keys=True)`.
- `receipts: (receipt) => void` hands you each receipt object (send it to your logger, database or OTel).
- `receipts: false` writes nothing. Default: the policy's `receipts`, else nothing (serverless-friendly).

`args_digest` is `sha256:` plus the first 32 hex characters of SHA-256 over the canonical JSON of the
arguments (sorted keys, `", "` / `": "` separators, no ASCII escaping), identical to Python's
`arguments_digest`. The same call digests the same way in both languages, so receipts from a TypeScript
agent and a Python agent can be joined.

## Parity guarantee

`test/fixtures/parity.json` is generated by running the Python reference implementation
(`scripts/gen_parity.py`, `benchpress.shims.guard_policy`). The TypeScript test suite asserts, case by case,
the same decision, rule, class, reason, `policy_rule`, refusal string, argument digest and byte-identical
receipt line (80 cases: rule order, globs, Python regex syntax, `max_calls`, reads denied, quotes in tool
names, floats, Unicode and astral-plane key ordering, invalid policies). Regenerate after changing the Python
guard with `npm run parity:generate`.

## Limits

- **No read-back.** The guard decides and records. It does not verify what an allowed write changed; that is
  the Benchpress loop's job.
- **Classification is declared or read from the name**, never inferred from behaviour.
- **Argument constraints see top-level arguments only.**
- **Regexes run on JavaScript's `RegExp`.** `(?P<name>…)`, `(?P=name)`, `\A`, `\Z` and leading `(?i)` / `(?m)` / `(?s)`
  are translated; patterns JavaScript cannot compile are rejected at load. `\d`, `\w`, `\s` are ASCII in
  JavaScript but Unicode in Python, so keep constraints ASCII when the same policy governs both.
- **Numbers.** JavaScript cannot tell `2` from `2.0`: whole numbers digest and render like Python `int`,
  others like Python `float`. Integers beyond 2^53 lose precision before the guard sees them.
- **No policy packs.** The Python shims can also judge calls with Benchpress policy packs; this package
  implements the rules only.
- `max_calls` counters live as long as the guard (one `guardTools` or `createGuard` call). Receipts are kept in
  memory on the guard for its lifetime.
- Node only (`node:crypto`, `node:fs`); not for edge runtimes.

## License

Apache-2.0. Part of [Benchpress](https://github.com/rajkaria/benchpress), the reliability layer for AI agents
with write access.
