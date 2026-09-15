import { describe, expect, it } from "vitest";
import { detailHash, listHash, parseHash } from "../route";

describe("hash routes", () => {
  it("round-trips list filters", () => {
    const hash = listHash({ provider: "hubspot", rule: "protected" });
    expect(parseHash(hash)).toEqual({ name: "list", filters: { provider: "hubspot", rule: "protected" } });
  });
  it("parses a detail route and falls back to the list", () => {
    expect(parseHash(detailHash("abc123"))).toEqual({ name: "detail", id: "abc123" });
    expect(parseHash("")).toEqual({ name: "list", filters: {} });
    expect(parseHash("#/nonsense")).toEqual({ name: "list", filters: {} });
  });
});
