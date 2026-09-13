import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { tool } from "ai";
import { describe, expect, it } from "vitest";
import { z } from "zod";
import { createGuard, guardTools, loadPolicy, PolicyError, type Receipt } from "../src/index.js";
import { pyFloatRepr, pyJsonDumps, pyRepr } from "../src/python.js";

const callOptions = { toolCallId: "call-1", messages: [], context: {} };

function invoiceTools(log: string[]) {
  return {
    getInvoice: tool({
      description: "Read one invoice",
      inputSchema: z.object({ invoiceId: z.string() }),
      execute: async ({ invoiceId }) => {
        log.push(`get ${invoiceId}`);
        return { invoiceId, status: "draft" };
      },
    }),
    updateInvoice: tool({
      description: "Set an invoice status",
      inputSchema: z.object({ invoiceId: z.string(), status: z.string() }),
      execute: async ({ invoiceId, status }) => {
        log.push(`update ${invoiceId} ${status}`);
        return { invoiceId, status };
      },
    }),
    deleteInvoice: tool({
      description: "Delete an invoice",
      inputSchema: z.object({ invoiceId: z.string() }),
      execute: async ({ invoiceId }) => {
        log.push(`delete ${invoiceId}`);
        return { deleted: invoiceId };
      },
    }),
    askHuman: tool({ description: "Client-side tool, no execute", inputSchema: z.object({ question: z.string() }) }),
  };
}

describe("guardTools on AI SDK tools (execute called directly, no model)", () => {
  it("runs allowed calls, refuses the rest before execute, keeps keys and non-executable tools", async () => {
    const log: string[] = [];
    const tools = invoiceTools(log);
    const receipts: Receipt[] = [];
    const guarded = guardTools(
      tools,
      { rules: [{ tool: "updateInvoice", arguments: { status: "sent|paid" }, max_calls: 1 }] },
      { receipts: (receipt) => receipts.push(receipt) },
    );

    expect(Object.keys(guarded)).toEqual(Object.keys(tools));
    expect(guarded.askHuman).toBe(tools.askHuman);
    expect(guarded.getInvoice).not.toBe(tools.getInvoice);
    expect(guarded.getInvoice.inputSchema).toBe(tools.getInvoice.inputSchema);
    expect(guarded.getInvoice.description).toBe("Read one invoice");

    expect(await guarded.getInvoice.execute({ invoiceId: "INV-7" }, callOptions)).toEqual({ invoiceId: "INV-7", status: "draft" });
    expect(await guarded.updateInvoice.execute({ invoiceId: "INV-7", status: "paid" }, callOptions)).toEqual({ invoiceId: "INV-7", status: "paid" });
    expect(await guarded.updateInvoice.execute({ invoiceId: "INV-7", status: "sent" }, callOptions)).toBe(
      "benchpress refused 'updateInvoice' [max_calls]: policy rule 0 ('updateInvoice') allows at most 1 call(s)",
    );
    expect(await guarded.updateInvoice.execute({ invoiceId: "INV-7", status: "void" }, callOptions)).toBe(
      "benchpress refused 'updateInvoice' [arguments_mismatch]: argument 'status' does not match 'sent|paid'",
    );
    expect(await guarded.deleteInvoice.execute({ invoiceId: "INV-7" }, callOptions)).toBe(
      "benchpress refused 'deleteInvoice' [no_allow_rule]: 'deleteInvoice' is classified destructive and no policy rule allows it",
    );

    expect(log).toEqual(["get INV-7", "update INV-7 paid"]);
    expect(receipts.map((r) => [r.tool, r.class, r.decision, r.rule, r.upstream_error])).toEqual([
      ["getInvoice", "read", "allow", "read", false],
      ["updateInvoice", "write", "allow", "allow_rule", false],
      ["updateInvoice", "write", "refuse", "max_calls", null],
      ["updateInvoice", "write", "refuse", "arguments_mismatch", null],
      ["deleteInvoice", "destructive", "refuse", "no_allow_rule", null],
    ]);
    for (const receipt of receipts) {
      expect(receipt.ts).toMatch(/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}\+00:00$/);
      expect(receipt.args_digest).toMatch(/^sha256:[0-9a-f]{32}$/);
      expect(JSON.stringify(receipt)).not.toContain("INV-7");
    }
  });

  it("appends JSONL receipts to a file, relative policy receipts resolve next to the policy file", async () => {
    const dir = mkdtempSync(join(tmpdir(), "benchpress-guard-"));
    const policyPath = join(dir, "guard.json");
    writeFileSync(policyPath, JSON.stringify({ receipts: "logs/receipts.jsonl", classes: { askHuman: "read" } }));
    const guarded = guardTools(invoiceTools([]), policyPath);
    await guarded.getInvoice.execute({ invoiceId: "A" }, callOptions);
    await guarded.deleteInvoice.execute({ invoiceId: "A" }, callOptions);
    const lines = readFileSync(join(dir, "logs", "receipts.jsonl"), "utf8").trim().split("\n");
    expect(lines).toHaveLength(2);
    const parsed = lines.map((line) => JSON.parse(line) as Record<string, unknown>);
    expect(Object.keys(parsed[0]!)).toEqual([
      "args_digest", "class", "decision", "latency_ms", "policy_rule", "reason", "rule", "tool", "ts", "upstream_error",
    ]);
    expect(parsed[1]!["decision"]).toBe("refuse");

    const explicit = join(dir, "explicit.jsonl");
    await guardTools(invoiceTools([]), policyPath, { receipts: explicit }).getInvoice.execute({ invoiceId: "B" }, callOptions);
    expect(readFileSync(explicit, "utf8").split("\n")).toHaveLength(2);
  });

  it("records upstream errors and rethrows, for sync throws, rejections and streams", async () => {
    const receipts: Receipt[] = [];
    const guard = createGuard({ rules: [{ tool: "*" }] }, { receipts: (r) => receipts.push(r) });
    const throwing = guard.wrap("sync_thing", { execute: (_input: unknown): string => { throw new Error("boom"); } });
    expect(() => throwing.execute({})).toThrow("boom");
    const rejecting = guard.wrap("async_thing", { execute: async (_input: unknown): Promise<string> => { throw new Error("nope"); } });
    await expect(rejecting.execute({})).rejects.toThrow("nope");

    const streaming = guard.wrap("stream_thing", {
      execute: async function* (_input: unknown) {
        yield "working";
        yield "done";
      },
    });
    const chunks: unknown[] = [];
    const iterable = streaming.execute({}) as AsyncIterable<unknown>;
    expect(typeof iterable[Symbol.asyncIterator]).toBe("function");
    for await (const chunk of iterable) chunks.push(chunk);
    expect(chunks).toEqual(["working", "done"]);
    expect(receipts.map((r) => [r.tool, r.upstream_error])).toEqual([
      ["sync_thing", true],
      ["async_thing", true],
      ["stream_thing", false],
    ]);
  });

  it("shares max_calls counters across tool sets wrapped by one guard", async () => {
    const guard = createGuard({ rules: [{ tool: "send_*", max_calls: 2 }] }, { receipts: false });
    const a = guard.wrapAll({ send_email: { execute: async () => "sent" } });
    const b = guard.wrapAll({ send_sms: { execute: async () => "sent" } });
    expect(await a.send_email.execute()).toBe("sent");
    expect(await b.send_sms.execute()).toBe("sent");
    expect(await a.send_email.execute()).toMatch(/^benchpress refused 'send_email' \[max_calls\]/);
    expect(guard.receipts).toHaveLength(3);
  });

  it("declared classes win over policy classes and the name heuristic", async () => {
    const guard = createGuard({ classes: { get_report: "write", purge_cache: "read" } }, { classes: { get_report: "destructive" }, receipts: false });
    expect(guard.classify("get_report")).toBe("destructive");
    expect(guard.classify("purge_cache")).toBe("read");
    expect(guard.classify("list_things")).toBe("read");
    const guarded = guard.wrap("purge_cache", { execute: async () => "purged" });
    expect(await guarded.execute()).toBe("purged");
  });
});

describe("loadPolicy", () => {
  it("fills defaults", () => {
    const policy = loadPolicy({ rules: [{ tool: "x" }] });
    expect(policy.reads).toBe("allow");
    expect(policy.receipts).toBeNull();
    expect(policy.rules[0]).toMatchObject({ tool: "x", effect: "allow", max_calls: null, allow_destructive: false, reason: "" });
  });

  it("names the location of the problem", () => {
    expect(() => loadPolicy({ rules: [{ tool: "x", effect: "block" as "allow" }] })).toThrow(
      'benchpress-guard: invalid policy: rules[0].effect must be "allow" or "deny", got "block"',
    );
    expect(() => loadPolicy({ rules: [{ tool: "x", arguments: { id: "(" } }] })).toThrow(/rules\[0\]\.arguments\.id: invalid regex/);
    expect(() => loadPolicy("/definitely/not/here.json")).toThrow(PolicyError);
    expect(() => loadPolicy({ rule: [] } as never)).toThrow(/unknown key "rule"/);
  });
});

describe("python reproductions", () => {
  it("repr(str)", () => {
    expect(pyRepr("plain")).toBe("'plain'");
    expect(pyRepr("don't")).toBe(`"don't"`);
    expect(pyRepr(`say "hi" don't`)).toBe(`'say "hi" don\\'t'`);
    expect(pyRepr("a\\b\n\u0001\u00ad")).toBe("'a\\\\b\\n\\x01\\xad'");
    expect(pyRepr("Zoë 🚀")).toBe("'Zoë 🚀'");
  });

  it("float repr", () => {
    expect([1.5, 1e-7, 1e16, 1e15 + 0.5, 0.0001, 0.00001, 2, -0, 5e-324, 1.7976931348623157e308].map(pyFloatRepr)).toEqual([
      "1.5", "1e-07", "1e+16", "1000000000000000.5", "0.0001", "1e-05", "2.0", "-0.0", "5e-324", "1.7976931348623157e+308",
    ]);
    expect(pyJsonDumps({ n: NaN, i: -Infinity })).toBe('{"i": -Infinity, "n": NaN}');
  });
});
