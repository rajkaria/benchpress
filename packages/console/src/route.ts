/** Hash routing: `#/?provider=hubspot` (list) or `#/receipts/<id>` (detail). No SPA fallback needed. */

export type Filters = {
  customer?: string;
  provider?: string;
  rule?: string;
  status?: string;
  session?: string;
  event?: string;
  limit?: number;
  before?: string;
};

export type Route = { name: "list"; filters: Filters } | { name: "detail"; id: string };

const EMPTY_LIST_ROUTE: Route = { name: "list", filters: {} };

/** Strips a leading `#`, then a leading `/`, leaving whatever the app put after `#/`. */
function stripHashPrefix(hash: string): string {
  let rest = hash;
  if (rest.startsWith("#")) rest = rest.slice(1);
  if (rest.startsWith("/")) rest = rest.slice(1);
  return rest;
}

export function parseHash(hash: string): Route {
  const rest = stripHashPrefix(hash);
  if (rest === "" || rest.startsWith("?")) {
    const query = rest.startsWith("?") ? rest.slice(1) : "";
    const params = new URLSearchParams(query);
    const filters: Filters = {};
    for (const [key, value] of params) {
      if (key === "limit") {
        const n = Number(value);
        if (Number.isFinite(n)) filters.limit = n;
        continue;
      }
      if (key === "customer" || key === "provider" || key === "rule" || key === "status" ||
          key === "session" || key === "event" || key === "before") {
        filters[key] = value;
      }
    }
    return { name: "list", filters };
  }
  const detailMatch = /^receipts\/(.+)$/.exec(rest);
  if (detailMatch) {
    const id = detailMatch[1];
    if (id) return { name: "detail", id: decodeURIComponent(id) };
  }
  return EMPTY_LIST_ROUTE;
}

export function listHash(filters: Filters): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value === undefined || value === "") continue;
    params.set(key, String(value));
  }
  const query = params.toString();
  return query ? `#/?${query}` : "#/";
}

export function detailHash(id: string): string {
  return `#/receipts/${encodeURIComponent(id)}`;
}
