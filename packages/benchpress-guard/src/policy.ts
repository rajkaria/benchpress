import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { compilePythonFullmatch } from "./python.js";

/** How a tool call touches the world. */
export type ToolClass = "read" | "write" | "destructive";

/** The rule that produced a decision, identical to the Python `DecisionRule` values this shim can emit. */
export type DecisionRule =
  | "read"
  | "allow_rule"
  | "invalid_arguments"
  | "deny_rule"
  | "arguments_mismatch"
  | "max_calls"
  | "destructive_default_deny"
  | "no_allow_rule"
  | "reads_denied";

/** One policy rule. `tool` is a glob over tool names (`create_*`, `*`). */
export interface GuardRule {
  tool: string;
  effect?: "allow" | "deny";
  /** Top-level argument name -> Python-style regex that must fully match the value. */
  arguments?: Record<string, string>;
  max_calls?: number | null;
  allow_destructive?: boolean;
  reason?: string;
}

/** A guard policy, the same JSON file `benchpress mcp-guard` and the OpenAI Agents shim read. */
export interface GuardPolicy {
  rules?: GuardRule[];
  classes?: Record<string, ToolClass>;
  reads?: "allow" | "deny";
  receipts?: string | null;
}

/** A validated policy with defaults filled in and argument regexes compiled. */
export interface LoadedPolicy {
  rules: LoadedRule[];
  classes: Record<string, ToolClass>;
  reads: "allow" | "deny";
  receipts: string | null;
  /** Directory the policy's relative `receipts` path resolves against. */
  baseDir: string;
}

export interface LoadedRule {
  tool: string;
  effect: "allow" | "deny";
  arguments: [name: string, pattern: string, compiled: RegExp][];
  max_calls: number | null;
  allow_destructive: boolean;
  reason: string;
}

/** Thrown when a policy file or object does not match the guard policy format. */
export class PolicyError extends Error {
  constructor(message: string) {
    super(`benchpress-guard: invalid policy: ${message}`);
    this.name = "PolicyError";
  }
}

const POLICY_KEYS = new Set(["rules", "classes", "reads", "receipts"]);
const RULE_KEYS = new Set(["tool", "effect", "arguments", "max_calls", "allow_destructive", "reason"]);
const CLASSES = new Set(["read", "write", "destructive"]);

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function describe(value: unknown): string {
  return JSON.stringify(value) ?? String(value);
}

/**
 * Validate a guard policy: a path to the JSON file, its parsed object, or an already loaded policy.
 * Unknown keys are rejected (as the Python model does with `extra="forbid"`), every error names its location.
 */
export function loadPolicy(policy: string | GuardPolicy | LoadedPolicy): LoadedPolicy {
  if (typeof policy === "string") {
    let text: string;
    try {
      text = readFileSync(policy, "utf8");
    } catch (error) {
      throw new PolicyError(`cannot read ${policy} (${(error as Error).message})`);
    }
    let parsed: unknown;
    try {
      parsed = JSON.parse(text);
    } catch (error) {
      throw new PolicyError(`${policy} is not valid JSON (${(error as Error).message})`);
    }
    return validate(parsed, dirname(resolve(policy)));
  }
  if (isLoaded(policy)) return policy;
  return validate(policy, process.cwd());
}

function isLoaded(value: unknown): value is LoadedPolicy {
  return isPlainObject(value) && typeof value["baseDir"] === "string" && Array.isArray(value["rules"]);
}

function validate(raw: unknown, baseDir: string): LoadedPolicy {
  if (!isPlainObject(raw)) throw new PolicyError(`the policy must be a JSON object, got ${describe(raw)}`);
  for (const key of Object.keys(raw)) {
    if (!POLICY_KEYS.has(key)) throw new PolicyError(`unknown key ${describe(key)} (allowed: rules, classes, reads, receipts)`);
  }
  const rulesRaw = raw["rules"] ?? [];
  if (!Array.isArray(rulesRaw)) throw new PolicyError("rules must be an array");
  const rules = rulesRaw.map((rule, index) => validateRule(rule, index));

  const classesRaw = raw["classes"] ?? {};
  if (!isPlainObject(classesRaw)) throw new PolicyError("classes must be an object of tool name -> class");
  const classes: Record<string, ToolClass> = {};
  for (const [name, value] of Object.entries(classesRaw)) {
    if (typeof value !== "string" || !CLASSES.has(value)) {
      throw new PolicyError(`classes.${name} must be "read", "write" or "destructive", got ${describe(value)}`);
    }
    classes[name] = value as ToolClass;
  }

  const reads = raw["reads"] ?? "allow";
  if (reads !== "allow" && reads !== "deny") throw new PolicyError(`reads must be "allow" or "deny", got ${describe(reads)}`);

  const receipts = raw["receipts"] ?? null;
  if (receipts !== null && typeof receipts !== "string") throw new PolicyError("receipts must be a string path or null");

  return { rules, classes, reads, receipts, baseDir };
}

function validateRule(raw: unknown, index: number): LoadedRule {
  const at = `rules[${index}]`;
  if (!isPlainObject(raw)) throw new PolicyError(`${at} must be an object`);
  for (const key of Object.keys(raw)) {
    if (!RULE_KEYS.has(key)) {
      throw new PolicyError(`${at}: unknown key ${describe(key)} (allowed: ${[...RULE_KEYS].join(", ")})`);
    }
  }
  const tool = raw["tool"];
  if (typeof tool !== "string" || tool.length === 0) throw new PolicyError(`${at}.tool must be a non-empty string`);
  const effect = raw["effect"] ?? "allow";
  if (effect !== "allow" && effect !== "deny") throw new PolicyError(`${at}.effect must be "allow" or "deny", got ${describe(effect)}`);

  const argumentsRaw = raw["arguments"] ?? {};
  if (!isPlainObject(argumentsRaw)) throw new PolicyError(`${at}.arguments must be an object of argument name -> regex`);
  const compiled: LoadedRule["arguments"] = [];
  for (const [name, pattern] of Object.entries(argumentsRaw)) {
    if (typeof pattern !== "string") throw new PolicyError(`${at}.arguments.${name} must be a regex string`);
    try {
      compiled.push([name, pattern, compilePythonFullmatch(pattern)]);
    } catch (error) {
      throw new PolicyError(`${at}.arguments.${name}: invalid regex ${describe(pattern)} (${(error as Error).message})`);
    }
  }

  const maxCalls = raw["max_calls"] ?? null;
  if (maxCalls !== null && (typeof maxCalls !== "number" || !Number.isInteger(maxCalls) || maxCalls < 0)) {
    throw new PolicyError(`${at}.max_calls must be an integer >= 0 or null, got ${describe(maxCalls)}`);
  }
  const allowDestructive = raw["allow_destructive"] ?? false;
  if (typeof allowDestructive !== "boolean") throw new PolicyError(`${at}.allow_destructive must be a boolean`);
  const reason = raw["reason"] ?? "";
  if (typeof reason !== "string") throw new PolicyError(`${at}.reason must be a string`);

  return { tool, effect, arguments: compiled, max_calls: maxCalls, allow_destructive: allowDestructive, reason };
}
