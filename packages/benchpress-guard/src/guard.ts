import { createHash } from "node:crypto";
import { appendFileSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { loadPolicy, type DecisionRule, type GuardPolicy, type LoadedPolicy, type ToolClass } from "./policy.js";
import { fnmatchCase, PyFloat, pyJsonDumps, pyRender, pyRepr } from "./python.js";

export const READ_VERBS: ReadonlySet<string> = new Set([
  "check", "count", "describe", "download", "export", "fetch", "find", "get", "inspect", "list",
  "load", "lookup", "peek", "preview", "query", "read", "retrieve", "search", "show", "summarize", "view",
]);
export const DESTRUCTIVE_VERBS: ReadonlySet<string> = new Set([
  "cancel", "chargeback", "delete", "destroy", "drop", "erase", "kill", "purge", "refund", "remove",
  "revoke", "terminate", "truncate", "void", "wipe",
]);

function nameTokens(name: string): string[] {
  const spaced = name.replace(/([a-z0-9])([A-Z])/g, "$1_$2");
  return spaced.toLowerCase().split(/[^a-z0-9]+/).filter((token) => token.length > 0);
}

/** A tool's class from its name: any destructive verb wins, then a leading read verb, else write. */
export function classifyToolName(name: string): ToolClass {
  const tokens = nameTokens(name);
  if (tokens.some((token) => DESTRUCTIVE_VERBS.has(token))) return "destructive";
  if (tokens.length > 0 && READ_VERBS.has(tokens[0]!)) return "read";
  return "write";
}

export interface Decision {
  allowed: boolean;
  toolClass: ToolClass;
  rule: DecisionRule;
  reason: string;
  /** Index of the policy rule that decided, when one did. */
  policyRule: number | null;
}

/** The JSONL receipt line, the same keys the Python shims write. Argument values are never stored. */
export interface Receipt {
  ts: string;
  tool: string;
  class: ToolClass;
  args_digest: string;
  decision: "allow" | "refuse";
  rule: DecisionRule;
  policy_rule: number | null;
  reason: string;
  latency_ms: number;
  /** `true` when execute threw or rejected, `false` when it returned, `null` when the call was refused. */
  upstream_error: boolean | null;
}

/** `sha256:` + the first 32 hex chars of sha256 over Python's canonical JSON of the arguments. */
export function argumentsDigest(args: Record<string, unknown>): string {
  return "sha256:" + createHash("sha256").update(pyJsonDumps(args), "utf8").digest("hex").slice(0, 32);
}

/** The string the model sees for a refused call, worded as the Python `refusal_message`. */
export function refusalMessage(name: string, decision: Pick<Decision, "rule" | "reason">): string {
  return `benchpress refused ${pyRepr(name)} [${decision.rule}]: ${decision.reason}`;
}

export type ReceiptSink = string | ((receipt: Receipt) => void) | false;

export interface GuardOptions {
  /**
   * A JSONL file path to append to, a callback per receipt, or `false` for none. Default: the policy's `receipts`
   * (relative to the policy file when loaded from a path, else the working directory), else no file.
   */
  receipts?: ReceiptSink;
  /** Tool name -> class. Wins over the policy's `classes` and the name heuristic. */
  classes?: Record<string, ToolClass>;
}

type ExecuteFunction = (input: never, options: never) => unknown;

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isThenable(value: unknown): value is PromiseLike<unknown> {
  return (typeof value === "object" || typeof value === "function") && value !== null && typeof (value as { then?: unknown }).then === "function";
}

function isAsyncIterable(value: unknown): value is AsyncIterable<unknown> {
  return typeof value === "object" && value !== null && typeof (value as { [Symbol.asyncIterator]?: unknown })[Symbol.asyncIterator] === "function";
}

function isoNow(): string {
  // Python `datetime.now(UTC).isoformat()`: microseconds and a +00:00 offset.
  return new Date().toISOString().replace(/\.(\d{3})Z$/, ".$1000+00:00");
}

/** One policy, its `max_calls` counters and its receipts, shared by every tool it wraps. */
export class ToolGuard {
  readonly policy: LoadedPolicy;
  readonly classes: Record<string, ToolClass>;
  /** Every receipt this guard produced, in order. */
  readonly receipts: Receipt[] = [];
  private readonly sink: string | ((receipt: Receipt) => void) | null;
  private readonly allowedByRule = new Map<number, number>();

  constructor(policy: string | GuardPolicy | LoadedPolicy, options: GuardOptions = {}) {
    this.policy = loadPolicy(policy);
    this.classes = { ...(options.classes ?? {}) };
    if (options.receipts === false) this.sink = null;
    else if (options.receipts !== undefined) this.sink = options.receipts;
    else this.sink = this.policy.receipts === null ? null : resolve(this.policy.baseDir, this.policy.receipts);
  }

  classify(name: string): ToolClass {
    return this.classes[name] || this.policy.classes[name] || classifyToolName(name);
  }

  /** Decide one call. `args` is what the AI SDK passes to `execute` (the parsed tool input). */
  decide(name: string, args: unknown): Decision {
    const toolClass = this.classify(name);
    const input = args === undefined ? {} : args;
    if (!isPlainObject(input)) {
      return { allowed: false, toolClass, rule: "invalid_arguments", reason: "arguments must be a JSON object", policyRule: null };
    }
    return this.decideClass(name, toolClass, input);
  }

  /** Decide one call of `name`, already classified. An allow counts against the rule's `max_calls`. */
  decideClass(name: string, toolClass: ToolClass, args: Record<string, unknown>): Decision {
    let mismatch: [number, string] | null = null;
    for (const [index, rule] of this.policy.rules.entries()) {
      if (!fnmatchCase(name, rule.tool)) continue;
      if (rule.effect === "deny") {
        const reason = rule.reason || `policy rule ${index} (${pyRepr(rule.tool)}) denies this tool`;
        return { allowed: false, toolClass, rule: "deny_rule", reason, policyRule: index };
      }
      const problem = argumentMismatch(rule.tool, rule.arguments, args);
      if (problem !== null) {
        mismatch = mismatch ?? [index, problem];
        continue;
      }
      if (rule.max_calls !== null && (this.allowedByRule.get(index) ?? 0) >= rule.max_calls) {
        const reason = `policy rule ${index} (${pyRepr(rule.tool)}) allows at most ${rule.max_calls} call(s)`;
        return { allowed: false, toolClass, rule: "max_calls", reason, policyRule: index };
      }
      if (toolClass === "destructive" && !rule.allow_destructive) {
        const reason = `${pyRepr(name)} is destructive; destructive tools are refused unless a rule sets allow_destructive`;
        return { allowed: false, toolClass, rule: "destructive_default_deny", reason, policyRule: index };
      }
      this.allowedByRule.set(index, (this.allowedByRule.get(index) ?? 0) + 1);
      return { allowed: true, toolClass, rule: "allow_rule", reason: rule.reason || `allowed by policy rule ${index}`, policyRule: index };
    }
    if (toolClass === "read") {
      if (this.policy.reads === "allow") {
        return { allowed: true, toolClass, rule: "read", reason: "read-only tool; reads are allowed by policy", policyRule: null };
      }
      return { allowed: false, toolClass, rule: "reads_denied", reason: "the policy denies reads without an allow rule", policyRule: null };
    }
    if (mismatch !== null) {
      return { allowed: false, toolClass, rule: "arguments_mismatch", reason: mismatch[1], policyRule: mismatch[0] };
    }
    const reason = `${pyRepr(name)} is classified ${toolClass} and no policy rule allows it`;
    return { allowed: false, toolClass, rule: "no_allow_rule", reason, policyRule: null };
  }

  /** Build, store and emit one receipt. */
  record(name: string, args: unknown, decision: Decision, started: number, upstreamError: boolean | null): Receipt {
    const receipt: Receipt = {
      ts: isoNow(),
      tool: name,
      class: decision.toolClass,
      args_digest: argumentsDigest(isPlainObject(args) ? args : {}),
      decision: decision.allowed ? "allow" : "refuse",
      rule: decision.rule,
      policy_rule: decision.policyRule,
      reason: decision.reason,
      latency_ms: Math.round((performance.now() - started) * 100) / 100,
      upstream_error: upstreamError,
    };
    this.receipts.push(receipt);
    if (typeof this.sink === "function") {
      this.sink(receipt);
    } else if (typeof this.sink === "string") {
      mkdirSync(dirname(this.sink), { recursive: true });
      appendFileSync(this.sink, receiptLine(receipt) + "\n", "utf8");
    }
    return receipt;
  }

  /** A copy of `tool` whose `execute` enforces this guard before the original runs. Tools without `execute` pass through. */
  wrap<T extends object>(name: string, tool: T): T {
    const inner = (tool as { execute?: unknown }).execute;
    if (typeof inner !== "function") return tool;
    const guard = this;
    const execute = function guardedExecute(this: unknown, input: unknown, options: unknown): unknown {
      const started = performance.now();
      const decision = guard.decide(name, input);
      if (!decision.allowed) {
        guard.record(name, input, decision, started, null);
        return refusalMessage(name, decision);
      }
      let result: unknown;
      try {
        result = (inner as (input: unknown, options: unknown) => unknown).call(this, input, options);
      } catch (error) {
        guard.record(name, input, decision, started, true);
        throw error;
      }
      if (isThenable(result)) {
        return Promise.resolve(result).then(
          (value) => {
            guard.record(name, input, decision, started, false);
            return value;
          },
          (error: unknown) => {
            guard.record(name, input, decision, started, true);
            throw error;
          },
        );
      }
      if (isAsyncIterable(result)) return recordStream(result, () => guard.record(name, input, decision, started, false), () => guard.record(name, input, decision, started, true));
      guard.record(name, input, decision, started, false);
      return result;
    };
    const copy = Object.create(Object.getPrototypeOf(tool), Object.getOwnPropertyDescriptors(tool)) as T;
    Object.defineProperty(copy, "execute", { value: execute as ExecuteFunction, enumerable: true, writable: true, configurable: true });
    return copy;
  }

  /** Guarded copies of every tool in the record, same keys. */
  wrapAll<TOOLS extends Record<string, object>>(tools: TOOLS): TOOLS {
    const out: Record<string, object> = {};
    for (const [name, tool] of Object.entries(tools)) out[name] = this.wrap(name, tool);
    return out as TOOLS;
  }
}

async function* recordStream(source: AsyncIterable<unknown>, onDone: () => void, onError: () => void): AsyncGenerator<unknown> {
  let recorded = false;
  try {
    for await (const item of source) yield item;
  } catch (error) {
    recorded = true;
    onError();
    throw error;
  } finally {
    if (!recorded) onDone();
  }
}

function argumentMismatch(tool: string, constraints: [string, string, RegExp][], args: Record<string, unknown>): string | null {
  for (const [name, pattern, compiled] of constraints) {
    if (!Object.prototype.hasOwnProperty.call(args, name)) {
      return `argument ${pyRepr(name)} is required by the rule for ${pyRepr(tool)}`;
    }
    if (!compiled.test(pyRender(args[name]))) return `argument ${pyRepr(name)} does not match ${pyRepr(pattern)}`;
  }
  return null;
}

/** The receipt as the Python shims serialize it: `json.dumps(line, ensure_ascii=False, sort_keys=True)`. */
export function receiptLine(receipt: Receipt): string {
  return pyJsonDumps({ ...receipt, latency_ms: new PyFloat(receipt.latency_ms) });
}

/** A guard to wrap tools with; keep it to share `max_calls` counters across tool sets or to read `guard.receipts`. */
export function createGuard(policy: string | GuardPolicy | LoadedPolicy, options: GuardOptions = {}): ToolGuard {
  return new ToolGuard(policy, options);
}

/**
 * Guarded copies of a Vercel AI SDK tools record (same keys). Every call is classified and checked against the
 * policy before the original `execute` runs; a refused call returns the refusal string and never executes.
 * All tools share one set of `max_calls` counters and one receipt sink.
 */
export function guardTools<TOOLS extends Record<string, object>>(
  tools: TOOLS,
  policy: string | GuardPolicy | LoadedPolicy,
  options: GuardOptions = {},
): TOOLS {
  return createGuard(policy, options).wrapAll(tools);
}
