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
 * Truthy/falsy reader for a `VITE_`-prefixed boolean flag. Mirrors {@link readEnv}
 * but coerces the string to a boolean: `"true"`/`"1"` (case-insensitive) ⇒ true,
 * anything else (including unset) ⇒ the supplied fallback. Exported as a pure
 * helper so feature gates can be unit-tested without touching `import.meta.env`.
 */
export function parseBoolFlag(value: unknown, fallback: boolean): boolean {
  if (typeof value !== 'string' || value.length === 0) return fallback;
  const normalized = value.trim().toLowerCase();
  if (normalized === 'true' || normalized === '1') return true;
  if (normalized === 'false' || normalized === '0') return false;
  return fallback;
}

function readBoolEnv(key: string, fallback: boolean): boolean {
  return parseBoolFlag(import.meta.env[key as keyof ImportMetaEnv], fallback);
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
 * session (the same RouteGuard that gates `/documents`). Each dev feature reads
 * this flag inside its own `route.tsx`/`nav.ts`, so the auto-discovery seam
 * (`routes/discovery.ts`, issue #79) drops the route + nav entry when OFF.
 *
 * Default precedence:
 *   - Production / dev builds: `false` unless `VITE_ENABLE_DEV_PAGES` is truthy.
 *   - Test runs (`MODE === 'test'`): default `true` so the auto-discovery and
 *     dev-page component suites can exercise the ON path without a committed
 *     `.env.test` (env files are git-ignored). The OFF path is covered by unit
 *     tests against the pure {@link parseBoolFlag} / gating predicate, not this
 *     module-level constant. `vite build` never sets `MODE==='test'`, so this
 *     never relaxes the production default.
 */
const DEV_PAGES_DEFAULT = import.meta.env.MODE === 'test';
export const DEV_PAGES_ENABLED: boolean = readBoolEnv('VITE_ENABLE_DEV_PAGES', DEV_PAGES_DEFAULT);
