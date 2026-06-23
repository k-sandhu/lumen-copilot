/**
 * Search feature route (ADR-0008 §3, issue #84) — `/search`, the
 * permission-trimmed cited search screen. Auto-discovered by
 * `routes/discovery.ts` via `import.meta.glob`; this module only declares the
 * route descriptor, so adding the screen touches ONLY this feature's files (never
 * routes/router.tsx).
 *
 * Lazy-loaded so the search bundle (incl. the markdown pipeline) stays out of the
 * main app chunk until visited. The guarded shell (auth + chrome) lives in
 * `components/SearchPage` — kept inside the slice so the route is self-contained.
 *
 * This module exports only the `route` descriptor (no component), but holds a
 * module-scope `lazy()` for code-splitting — so the react-refresh HMR heuristic
 * is disabled here; there is no component to fast-refresh.
 */
/* eslint-disable react-refresh/only-export-components */
import { lazy, Suspense } from 'react';
import { RouteFallback } from '@/routes/RouteFallback';
import type { FeatureRoute } from '@/routes/types';

const SearchPage = lazy(() =>
  import('./components/SearchPage').then((m) => ({ default: m.SearchPage })),
);

export const route: FeatureRoute = {
  path: '/search',
  element: (
    <Suspense fallback={<RouteFallback label="search" />}>
      <SearchPage />
    </Suspense>
  ),
  order: 15,
};
