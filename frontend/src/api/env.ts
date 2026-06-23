/**
 * The ONLY place runtime env is read for the api/ boundary. Only `VITE_`-prefixed
 * vars reach the client (Vite contract). Values come from `.env` (see
 * `.env.example`): VITE_API_BASE_URL=/api, VITE_WS_BASE_URL=/ws — same-origin
 * paths the Vite dev proxy forwards to the backend.
 */

function readEnv(key: string, fallback: string): string {
  const value = import.meta.env[key as keyof ImportMetaEnv];
  return typeof value === 'string' && value.length > 0 ? value : fallback;
}

/**
 * Truthy/falsy reader for a `VITE_`-prefixed boolean flag: `"true"`/`"1"`
 * (case-insensitive) ⇒ true, anything else (including unset) ⇒ the supplied
 * fallback. Pure, so the dev-pages gate's truth-table can be unit-tested without
 * touching `import.meta.env`. Mirrors the `parseDevPagesFlag` in `vite.config.ts`
 * that resolves the build-time literal — same precedence, one tested here.
 */
export function parseBoolFlag(value: unknown, fallback: boolean): boolean {
  if (typeof value !== 'string' || value.length === 0) return fallback;
  const normalized = value.trim().toLowerCase();
  if (normalized === 'true' || normalized === '1') return true;
  if (normalized === 'false' || normalized === '0') return false;
  return fallback;
}

/** REST base — the versioned API mount "/api/v1". Same-origin in dev via the Vite proxy. */
export const API_BASE_URL: string = readEnv('VITE_API_BASE_URL', '/api/v1');

/** WebSocket base, e.g. "/ws". Same-origin in dev via the Vite proxy. */
export const WS_BASE_URL: string = readEnv('VITE_WS_BASE_URL', '/ws');

/**
 * Developer-only pages (`/docs`, `/features`) gate (issue #40). OFF by default —
 * those pages render internal docs/ADRs/specs and the shipped-capabilities
 * catalog, which the wireframe IA (ADR-0007) excludes from the product surface
 * and which "permissioned by default / least exposure" (AGENTS.md §2/§4) keeps
 * out of production. When ON, the owning feature still requires an authenticated
 * session (the same RouteGuard that gates `/documents`).
 *
 * This is a **build-time `define`-injected literal** (`__DEV_PAGES_ENABLED__`,
 * set in `vite.config.ts` from `VITE_ENABLE_DEV_PAGES`). Exposing it as a literal
 * is the security property: each dev feature's `route.tsx`/`nav.ts` gates its
 * `lazy(() => import(...))` on the **literal**, so when OFF Rollup dead-code-
 * eliminates the whole branch — and the dev-page chunks, and the internal docs
 * the docs viewer inlines via `import.meta.glob`, are **not emitted at all**.
 * (The prior dynamic `import.meta.env[key]` read was opaque to the minifier, so a
 * flag-OFF build still shipped — and direct-URL-leaked — the docs chunk. The
 * `dist-no-dev-pages.test.ts` build-artifact test guards against regressing to
 * that.)
 *
 * Re-exported here as the canonical runtime view (e.g. for diagnostics); the
 * GATES read `__DEV_PAGES_ENABLED__` directly, never this binding, so an imported
 * binding can never block the dead-code elimination.
 */
export const DEV_PAGES_ENABLED: boolean = __DEV_PAGES_ENABLED__;
