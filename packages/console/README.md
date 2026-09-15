# @benchpress/console

The Benchpress console: a small React 19 + Vite + TypeScript single-page app that lists and inspects
receipts. It is built into `../../src/benchpress/console_dist` and served by both `benchpress serve`
(the gateway, mode `"gateway"`, requires an API key) and `benchpress ui` (local disk receipts, mode
`"local"`, no auth).

## Develop

```bash
cd packages/console
npm ci
npm run dev
```

`vite`'s dev server proxies `/v1` and `/healthz` to `http://127.0.0.1:8787`, so run `benchpress serve`
(or `benchpress ui`) on that port alongside it.

## Test

```bash
npm test        # vitest run (no watch mode)
npm run typecheck
```

## Build

```bash
npm run build
```

Writes the production build to `../../src/benchpress/console_dist`, which is committed: neither
`benchpress serve` nor `benchpress ui` builds the console themselves, so the repository ships a
ready-to-serve build. CI rebuilds with `npm ci && npm run build` and fails if the rebuilt output
differs from what is committed, so always commit `console_dist` together with any source change here.

Routing is hash-based (`#/`, `#/receipts/<id>`), so the static file server needs no SPA fallback route.
No UI/CSS libraries, no external fonts, CDNs, or analytics: every request other than the app's own
`/v1/*` calls would break the "no external requests" guarantee `tests/test_console_dist.py` checks for.
