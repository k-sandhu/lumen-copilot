/**
 * Feature-catalog route (ADR-0008 §3, issue #79) — `/features`, the standalone
 * catalog of what's been built. Lazy-loaded so the catalog stays out of the main
 * app chunk until visited (unchanged behavior from the prior router).
 *
 * Developer-only page (issue #40): gated behind `VITE_ENABLE_DEV_PAGES`
 * (`api/env.ts` → {@link DEV_PAGES_ENABLED}). When OFF (the production default)
 * this module exports `route: undefined`, which `routes/discovery.ts` filters out
 * of the manifest — so `/features` is neither in the nav nor routable (404),
 * without editing the shared discovery/router seam. When ON, the page is wrapped
 * in the same auth `RouteGuard` as `/documents`, so it still requires a session.
 *
 * This module exports only the `route` descriptor (no component), but holds a
 * module-scope `lazy()` for code-splitting — so the react-refresh HMR heuristic
 * is disabled here; there is no component to fast-refresh.
 */
/* eslint-disable react-refresh/only-export-components */
import { lazy, Suspense } from 'react';
import { DEV_PAGES_ENABLED } from '@/api';
import { RouteGuard } from '@/features/auth';
import { RouteFallback } from '@/routes/RouteFallback';
import type { FeatureRoute } from '@/routes/types';

const FeaturesPage = lazy(() => import('./index').then((m) => ({ default: m.FeaturesPage })));

/**
 * `undefined` when the dev-pages flag is OFF — auto-discovery drops a non-object
 * `route`, so the path never registers (issue #40). When ON, the catalog is
 * auth-gated like every other product page.
 */
export const route: FeatureRoute | undefined = DEV_PAGES_ENABLED
  ? {
      path: '/features',
      element: (
        <RouteGuard>
          <Suspense fallback={<RouteFallback label="features" />}>
            <FeaturesPage />
          </Suspense>
        </RouteGuard>
      ),
      order: 30,
    }
  : undefined;
