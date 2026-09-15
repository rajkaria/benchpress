/** The console's HTTP client: mirrors `benchpress.gateway.schemas` and never puts the key in a URL. */

import type { Filters } from "./route";

export type Meta = { mode: "gateway" | "local"; version: string; auth?: string };

export type ReceiptKind = "write" | "guard" | "run";

export type ReceiptSummary = {
  id: string;
  // Not `ReceiptKind`: a summary's kind is display-only (never a branch discriminant, unlike
  // `ReceiptDetail.kind`), so a plain string keeps a page of hand-built fixture data assignable
  // without a cast.
  kind: string;
  at: string;
  event: string;
  session: string;
  provider: string;
  method: string;
  path: string;
  status: string | null;
  rule: string;
  resource: string;
};

export type ReceiptPage = { receipts: ReceiptSummary[]; next_before: string | null };

export type ReceiptDetail = { id: string; kind: ReceiptKind; payload: Record<string, unknown>; html: boolean };

export type KeyStore = { get(): string | null; set(key: string): void; clear(): void };

const SESSION_KEY = "benchpress.apiKey";

export const sessionKeyStore: KeyStore = {
  get: () => window.sessionStorage.getItem(SESSION_KEY),
  set: (key: string) => window.sessionStorage.setItem(SESSION_KEY, key),
  clear: () => window.sessionStorage.removeItem(SESSION_KEY),
};

export class Unauthorized extends Error {
  constructor() {
    super("unauthorized");
    this.name = "Unauthorized";
  }
}

export type Api = {
  meta(): Promise<Meta>;
  list(f: Filters): Promise<ReceiptPage>;
  get(id: string): Promise<ReceiptDetail>;
  htmlUrl(id: string): string;
};

/** Query strings are built with `URLSearchParams`, omitting empty values, in insertion order. */
function query(f: Filters): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(f)) {
    if (value === undefined || value === null || value === "") continue;
    params.set(key, String(value));
  }
  const s = params.toString();
  return s ? `?${s}` : "";
}

export function createApi(fetchImpl: typeof fetch = fetch, keyStore: KeyStore = sessionKeyStore): Api {
  async function request(path: string): Promise<Response> {
    const key = keyStore.get();
    const headers = new Headers();
    if (key) headers.set("Authorization", `Bearer ${key}`);
    const response = await fetchImpl(path, { headers });
    if (response.status === 401) throw new Unauthorized();
    return response;
  }

  return {
    async meta(): Promise<Meta> {
      const response = await request("/v1/meta");
      return (await response.json()) as Meta;
    },
    async list(f: Filters): Promise<ReceiptPage> {
      const response = await request(`/v1/receipts${query(f)}`);
      return (await response.json()) as ReceiptPage;
    },
    async get(id: string): Promise<ReceiptDetail> {
      const response = await request(`/v1/receipts/${encodeURIComponent(id)}`);
      return (await response.json()) as ReceiptDetail;
    },
    htmlUrl(id: string): string {
      return `v1/receipts/${encodeURIComponent(id)}/html`;
    },
  };
}
