# frontend — Lumen Copilot SPA

React + Vite + TypeScript (strict) SPA. Static build; the only backend is FastAPI,
reached **same-origin** through the Vite dev proxy and the `src/api/` boundary.
Coding contract: [`AGENTS.md`](./AGENTS.md) (subordinate to the root `AGENTS.md`).

## Run

```bash
# Whole stack (from repo root) — frontend on http://localhost:47180
cp .env.example .env        # first time
docker compose up --build

# Inner loop (this dir), against a backend reachable at localhost:47181:
pnpm install
VITE_PROXY_TARGET=http://localhost:47181 pnpm dev   # http://localhost:5173
```

In compose the dev server binds `0.0.0.0:5173` and proxies `/health`, `/api`, and
`/ws` (ws:true) to `http://backend:8000`. Outside compose, set `VITE_PROXY_TARGET`.

## Layout (feature-sliced)

- `src/api/` — **the only place that talks to the backend.** `client.ts` (typed
  fetch, parses RFC-9457 `Problem` into `ApiError`), `health.ts` (`getReadiness`),
  `ws.ts` (typed WebSocket client: envelope parsing, lifecycle, backoff reconnect).
- `src/features/<name>/` — vertical slices (today: `system-status`). Components +
  hooks; consume `api/` via hooks, never raw transport.
- `src/components/` — shared Radix-based primitives (Card, StatusBadge, ScrollArea,
  ErrorBoundary). `src/lib/` — markdown renderer + utils. `src/stores/` — Zustand
  UI-only state (theme, rail). `src/routes/` — the two-pane shell + router.

## The `api/` boundary + `gen:api`

`contracts/openapi.yaml` is the **source of truth** for REST types. Generate them:

```bash
pnpm gen:api      # openapi-typescript ../contracts/openapi.yaml -> src/api/generated/schema.ts
```

`src/api/generated/` is **gitignored** (regenerated, never committed). So the app
also keeps a hand-authored mirror in `src/api/types.ts` to type-check before
`gen:api` runs — when they diverge, the generated output (the contract) wins.

## Quality bar (non-negotiable — see AGENTS.md / ADR-0006)

- **Every async state**, not just success: loading, empty, error (actionable
  retry), and streaming — no blank panes, no spinner that never resolves.
- **Rendered, never raw:** markdown goes through `lib/markdown.tsx` (sanitized +
  highlighted). Never `dangerouslySetInnerHTML` raw content.
- **Independently scrollable panes:** flex/grid with `min-h-0` + contained
  `overflow`; the composer stays pinned. Verify with real long content.
- Server state → TanStack Query; only true UI state → Zustand. TS strict, no `any`.

## Test / check

```bash
pnpm test         # Vitest + Testing Library (state coverage: loading/error/success)
pnpm test:e2e     # Playwright smoke (loads the app, asserts shell + status)
pnpm typecheck && pnpm lint
```
