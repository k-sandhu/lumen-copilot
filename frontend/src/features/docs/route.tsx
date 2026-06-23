/**
 * Docs feature route (ADR-0008 §3, issue #79) — `/docs/*`, the standalone
 * documentation viewer. Lazy-loaded so the docs bundle (every repo markdown file
 * via import.meta.glob) stays out of the main app chunk until visited. The splat
 * matches both `/docs` and `/docs/<slug…>` (unchanged from the prior router).
 *
 * This module exports only the `route` descriptor (no component), but holds a
 * module-scope `lazy()` for code-splitting — so the react-refresh HMR heuristic
 * is disabled here; there is no component to fast-refresh.
 */
/* eslint-disable react-refresh/only-export-components */
import { lazy, Suspense } from 'react';
import { RouteFallback } from '@/routes/RouteFallback';
import type { FeatureRoute } from '@/routes/types';

const DocsPage = lazy(() => import('./index').then((m) => ({ default: m.DocsPage })));

export const route: FeatureRoute = {
  path: '/docs/*',
  element: (
    <Suspense fallback={<RouteFallback label="documentation" />}>
      <DocsPage />
    </Suspense>
  ),
  order: 10,
};
