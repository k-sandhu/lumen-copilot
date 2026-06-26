# frontend/AGENTS.md — frontend coding contract

> Area contract for `frontend/`. Subordinate to the root [`AGENTS.md`](../AGENTS.md); it **elaborates, never contradicts**. Read the root contract, [ADR-0003](../docs/architecture/0003-application-stack.md) (stack), and [ADR-0006](../docs/architecture/0006-contract-first-parallel-implementation.md) (the quality bar) first.

## Stack (fixed by ADR-0003)
**React + Vite** SPA, **TypeScript strict**, **`pnpm`**. **TanStack Query** (server state) + **Zustand** (local UI state). **React Router**. **Tailwind** + Radix-based components (owned in-repo). Markdown via **`react-markdown` + `rehype-sanitize`** + syntax highlighting. **Vitest** + Testing Library (unit/component), **Playwright** (E2E), ESLint + Prettier. The SPA is **static** — there is no Node backend; the only backend is FastAPI, reached through the contract.

## Layout — feature-sliced, modular
```
frontend/src/
  api/              # generated client (from contracts/ OpenAPI) + typed WS client. THE ONLY place that calls the backend.
  features/<name>/  # self-contained slice: components, hooks, model (store/queries), types. No cross-feature deep imports.
  components/       # shared, presentational, app-agnostic UI primitives (Radix-based)
  routes/           # route components wiring features together (React Router)
  stores/           # cross-cutting UI state (Zustand) — NOT a dumping ground for server data
  lib/              # markdown renderer, formatting, ws/reconnect, utils
  styles/           # Tailwind config, tokens
  main.tsx          # app entry
```
- **`api/` is the only backend caller** (ADR-0004). No `fetch`/`WebSocket` to the backend anywhere else; components consume hooks, not transport. The client is **generated** from `contracts/` — never hand-maintained.
- Features are **vertically sliced** and independently testable; shared logic graduates to `components/`/`lib/` deliberately, not by reaching across features.

### Wire types: `gen:api`, and the hand-authored stopgap (`api/types.ts`)
The canonical REST types are **generated** from the frozen contract, not hand-written:
```
pnpm gen:api   # openapi-typescript ../contracts/openapi.yaml -o src/api/generated/schema.ts
```
`src/api/generated/` is **gitignored and never committed** (`.gitignore` "Generated contract artifacts"; ADR-0004) — it is rebuilt from `contracts/openapi.yaml` on demand, so committing it would let it drift silently. Until the generated client is wired through everywhere, `src/api/types.ts` is a documented **hand-authored mirror** of the same contract so the app type-checks before `gen:api` runs.
- **When you touch a wire shape** (or after a `contracts/` change): run `pnpm gen:api` and reconcile any diff between the generated `schema.ts` and `api/types.ts`. **The contract wins** — update `api/types.ts` to match (or, better, re-export from the generated types). Do **not** commit `src/api/generated/`.
- CI does not see the generated output (it is gitignored), so this regenerate-and-verify step is the only guard against `api/types.ts` drifting from the contract — run it as part of any wire-touching change.
- Server state lives in **TanStack Query** (caching, refetch, invalidation); only genuinely client-side UI state (open panels, draft input, theme) lives in **Zustand**. Don't mirror server data into a store.

## The UX quality bar — non-negotiable (ADR-0006)
A view is **not done** when the happy path renders.

- **Rendered, never raw.** Model/markdown output goes through the sanitizing pipeline (`react-markdown` + `rehype-sanitize`): real code blocks (with copy + highlight), tables, lists, links. **Never** dump a raw markdown string and never `dangerouslySetInnerHTML` unsanitized content.
- **Every state, not just success.** Design and implement **loading, empty, error, partial, and streaming-in-progress** for every async surface. No blank panes; no spinner that can never resolve; errors are actionable (retry), not dead ends.
- **Independently scrollable panes.** Multi-pane layouts (e.g. conversation + sources/citations, list + detail) each scroll **independently**; the composer/input stays pinned; long content, long code lines, and long sessions never break layout or force whole-page scroll. Use proper flex/grid with `min-height:0` and contained overflow — verify with real long content, not a one-line stub.
- **Streaming UX.** Token streams render incrementally and smoothly (no layout thrash / scroll-jank); autoscroll yields to the user when they scroll up; the stream is **cancellable** (stop button + on navigation/disconnect); reconnect with backoff resumes or fails gracefully.
- **Accessible & responsive.** Keyboard-navigable, visible focus, managed focus on async updates, ARIA labels/roles, color-contrast; usable from narrow widths up. Honors `prefers-reduced-motion`.
- **Resilient.** Error boundaries around feature roots; transient failures retry; nothing leaves the UI wedged.

## WebSocket client (`api/` + `lib/`)
- One typed WS client implementing the `contracts/` envelopes; reconnect with backoff + jitter; explicit lifecycle (connecting / open / closing / closed) surfaced to the UI. Generation is cancellable from the client. Features consume a hook, not the raw socket.

## TypeScript & quality
- `strict` on; **no `any`** (use `unknown` + narrowing). Props and API types come from generated/contract types — don't re-declare wire shapes.
- Pure, presentational components where possible; side effects in hooks. Keep components small and composable. No business logic in JSX branches that a reducer/hook should own.

## Testing (test-first where it pays — root §9)
- Component tests (Testing Library) cover **each state** (loading/empty/error/success/streaming), not just success. Critical flows (send message → stream → render with citations) get a Playwright E2E. Test against the generated client/mocks so a contract match is an integration match (ADR-0006 Phase 1).
- Accessibility assertions (roles/labels/focus) on interactive components.

## Definition of Done (frontend slice — also root §15 and ADR-0006 quality bar)
- Consumes the frozen `contracts/` via the generated client only; no stray transport.
- Every async surface implements loading/empty/error/partial/streaming; markdown rendered+sanitized; panes independently scrollable verified with long content; cancellable streams.
- Keyboard + a11y pass; responsive at narrow widths.
- Tests (state coverage + a critical E2E) green; ESLint/Prettier/`tsc` clean.
- `docker compose up` still converges (frontend loads and reaches the backend).
