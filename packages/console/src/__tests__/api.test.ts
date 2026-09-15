import { describe, expect, it, vi } from "vitest";
import { createApi, Unauthorized, type KeyStore } from "../api";

function memoryKeys(initial: string | null = null): KeyStore {
  let key = initial;
  return { get: () => key, set: (k) => { key = k; }, clear: () => { key = null; } };
}

describe("api client", () => {
  it("sends filters as query and the key as a bearer header", async () => {
    const fetchImpl = vi.fn(async () => new Response(JSON.stringify({ receipts: [], next_before: null }), { status: 200 }));
    const api = createApi(fetchImpl as unknown as typeof fetch, memoryKeys("bp_test"));
    await api.list({ provider: "hubspot", status: "verified" });
    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/v1/receipts?provider=hubspot&status=verified");
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer bp_test");
  });
  it("throws Unauthorized on 401 and sends no header without a key", async () => {
    const fetchImpl = vi.fn(async () => new Response("{}", { status: 401 }));
    const api = createApi(fetchImpl as unknown as typeof fetch, memoryKeys());
    await expect(api.meta()).rejects.toBeInstanceOf(Unauthorized);
    const [, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(new Headers(init.headers).has("Authorization")).toBe(false);
  });
  it("builds relative html urls", () => {
    expect(createApi(fetch, memoryKeys()).htmlUrl("a b")).toBe("v1/receipts/a%20b/html");
  });
  it("never puts the key in a request URL", async () => {
    const fetchImpl = vi.fn(async () => new Response(JSON.stringify({ receipts: [], next_before: null }), { status: 200 }));
    const api = createApi(fetchImpl as unknown as typeof fetch, memoryKeys("bp_test"));
    await api.list({ provider: "hubspot" });
    for (const call of fetchImpl.mock.calls) {
      const [url] = call as unknown as [string];
      expect(url).not.toContain("bp_test");
    }
  });
});
