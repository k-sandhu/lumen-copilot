/**
 * Docs feature route (ADR-0008 §3, issue #79) — `/docs/*`, the standalone
 * documentation viewer. Lazy-loaded so the docs bundle (every repo markdown file
 * via import.meta.glob) stays out of the main app chunk until visited. The splat
 * matches both `/docs` and `/docs/<slug…>` (unchanged from the prior router).
 *
 * Developer-only page (issue #40) — BUILD-TIME gated behind `VITE_ENABLE_DEV_PAGES`
 * (`vite.config.ts` → the `define`-injected literal {@link __DEV_PAGES_ENABLED__}).
 * The gate is a compile-time *literal*, and the lazy `import('./index')` lives
 * INSIDE the gated branch, so when the flag is OFF (the production default) Rollup
 * dead-code-eliminates the whole branch — dropping the DocsPage chunk AND the
 * internal docs the viewer inlines via `import.meta.glob`. The module then exports
 * `route: undefined`, which `routes/discovery.ts` filters out, so `/docs` is also
 * absent from the nav and unroutable (404). When ON, the page is wrapped in the
 * same auth `RouteGuard` as `/documents`, so it still requires a session.
 *
 * Gating on the literal (not a runtime `DEV_PAGES_ENABLED`) is the security
 * property: a runtime gate hides the route but still SHIPS the chunk, and static
 * JS is fetchable by direct URL regardless of auth.
 *
 * This module exports only the `route` descriptor (no component), but holds a
 * `lazy()` for code-splitting — so the react-refresh HMR heuristic is disabled
 * here; there is no component to fast-refresh.
 */
/* eslint-disable react-refresh/only-export-components */
import { lazy, Suspense } from 'react';
import { RouteGuard } from '@/features/auth';
import { RouteFallback } from '@/routes/RouteFallback';
import type { FeatureRoute } from '@/routes/types';

/**
 * Builds the `/docs/*` route. Called ONLY from inside the `__DEV_PAGES_ENABLED__`
 * branch below, so the `lazy(() => import('./index'))` it holds is unreachable
 * (and dropped, with its docs chunk) whenever the literal is `false`.
 */
function buildDocsRoute(): FeatureRoute {
  const DocsPage = lazy(() => import('./index').then((m) => ({ default: m.DocsPage })));
  return {
    path: '/docs/*',
    element: (
      <RouteGuard>
        <Suspense fallback={<RouteFallback label="documentation" />}>
          <DocsPage />
        </Suspense>
      </RouteGuard>
    ),
    order: 10,
  };
}

/**
 * `undefined` when the dev-pages flag is OFF — auto-discovery drops a non-object
 * `route`, so the path never registers and the chunk is never emitted (issue
 * #40). When ON, the viewer is auth-gated like every other product page.
 */
export const route: FeatureRoute | undefined = __DEV_PAGES_ENABLED__ ? buildDocsRoute() : undefined;
