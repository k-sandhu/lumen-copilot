/**
 * Feature-catalog route (ADR-0008 §3, issue #79) — `/features`, the standalone
 * catalog of what's been built. Lazy-loaded so the catalog stays out of the main
 * app chunk until visited (unchanged behavior from the prior router).
 *
 * This module exports only the `route` descriptor (no component), but holds a
 * module-scope `lazy()` for code-splitting — so the react-refresh HMR heuristic
 * is disabled here; there is no component to fast-refresh.
 */
/* eslint-disable react-refresh/only-export-components */
import { lazy, Suspense } from 'react';
import { RouteFallback } from '@/routes/RouteFallback';
import type { FeatureRoute } from '@/routes/types';

const FeaturesPage = lazy(() => import('./index').then((m) => ({ default: m.FeaturesPage })));

export const route: FeatureRoute = {
  path: '/features',
  element: (
    <Suspense fallback={<RouteFallback label="features" />}>
      <FeaturesPage />
    </Suspense>
  ),
  order: 30,
};
