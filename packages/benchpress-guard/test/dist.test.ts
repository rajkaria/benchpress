import { existsSync } from "node:fs";
import { createRequire } from "node:module";
import { describe, expect, it } from "vitest";

const esmPath = new URL("../dist/index.js", import.meta.url);
const cjsPath = new URL("../dist/index.cjs", import.meta.url);

describe("published builds (run `npm run build` first)", () => {
  it("ESM and CJS builds and their type declarations exist", () => {
    for (const file of ["index.js", "index.cjs", "index.d.ts", "index.d.cts"]) {
      expect(existsSync(new URL(`../dist/${file}`, import.meta.url)), `dist/${file} missing: run npm run build`).toBe(true);
    }
  });

  it("both formats export a working guardTools", async () => {
    const esm = (await import(esmPath.href)) as typeof import("../src/index.js");
    const cjs = createRequire(import.meta.url)(cjsPath.pathname) as typeof import("../src/index.js");
    for (const mod of [esm, cjs]) {
      const guarded = mod.guardTools({ delete_row: { execute: async () => "deleted" } }, {}, { receipts: false });
      expect(await guarded.delete_row.execute()).toBe(
        "benchpress refused 'delete_row' [no_allow_rule]: 'delete_row' is classified destructive and no policy rule allows it",
      );
    }
  });
});
