import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import {
  argumentsDigest,
  classifyToolName,
  createGuard,
  loadPolicy,
  PolicyError,
  receiptLine,
  refusalMessage,
  type GuardPolicy,
  type Receipt,
  type ToolClass,
} from "../src/index.js";

interface Expected {
  allowed: boolean;
  class: ToolClass;
  rule: string;
  reason: string;
  policy_rule: number | null;
  refusal: string | null;
  digest: string;
  receipt_line: string;
}

interface Fixture {
  fixed_ts: string;
  fixed_latency_ms: number;
  policies: { name: string; policy: GuardPolicy; calls: { tool: string; args: unknown; expected: Expected }[] }[];
  invalid_policies: unknown[];
  classify: { name: string; class: ToolClass }[];
  digests: { args: Record<string, unknown>; digest: string }[];
}

const fixture = JSON.parse(readFileSync(new URL("./fixtures/parity.json", import.meta.url), "utf8")) as Fixture;

describe("parity with the Python guard (test/fixtures/parity.json)", () => {
  it("has cases", () => {
    expect(fixture.policies.length).toBeGreaterThan(0);
    expect(fixture.classify.length).toBeGreaterThan(0);
    expect(fixture.digests.length).toBeGreaterThan(0);
  });

  for (const scenario of fixture.policies) {
    it(`decisions: ${scenario.name}`, async () => {
      const receipts: Receipt[] = [];
      const executed: string[] = [];
      const guard = createGuard(scenario.policy, { receipts: (receipt) => receipts.push(receipt) });
      for (const [index, call] of scenario.calls.entries()) {
        const where = `${scenario.name} #${index} ${call.tool}`;
        const tool = guard.wrap(call.tool, {
          execute: async (_input: unknown) => {
            executed.push(call.tool);
            return "ok";
          },
        });
        const result = await tool.execute(call.args);
        const expected = call.expected;
        const receipt = receipts.at(-1)!;
        expect({
          allowed: receipt.decision === "allow",
          class: receipt.class,
          rule: receipt.rule,
          reason: receipt.reason,
          policy_rule: receipt.policy_rule,
          digest: receipt.args_digest,
        }, where).toEqual({
          allowed: expected.allowed,
          class: expected.class,
          rule: expected.rule,
          reason: expected.reason,
          policy_rule: expected.policy_rule,
          digest: expected.digest,
        });
        expect(result, where).toBe(expected.allowed ? "ok" : expected.refusal);
        if (!expected.allowed) {
          expect(refusalMessage(call.tool, { rule: receipt.rule, reason: receipt.reason }), where).toBe(expected.refusal);
        }
        const line = receiptLine({ ...receipt, ts: fixture.fixed_ts, latency_ms: fixture.fixed_latency_ms });
        expect(line, where).toBe(expected.receipt_line);
      }
      expect(executed).toEqual(scenario.calls.filter((call) => call.expected.allowed).map((call) => call.tool));
    });
  }

  it("rejects every policy the Python model rejects", () => {
    for (const policy of fixture.invalid_policies) {
      expect(() => loadPolicy(policy as GuardPolicy), JSON.stringify(policy)).toThrow(PolicyError);
    }
  });

  it("classifies names like classify_tool_name", () => {
    for (const { name, class: expected } of fixture.classify) {
      expect(classifyToolName(name), JSON.stringify(name)).toBe(expected);
    }
  });

  it("digests arguments byte-for-byte like arguments_digest", () => {
    for (const { args, digest } of fixture.digests) {
      expect(argumentsDigest(args), JSON.stringify(args)).toBe(digest);
    }
  });
});
