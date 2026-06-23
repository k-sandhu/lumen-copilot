/**
 * Sources feature route (ADR-0008 §3, auto-discovered) — `/sources`, the connector
 * grid + sync-health screen (#27, ADR-0009, the 6th wireframe surface).
 * Auto-discovered by `routes/discovery.ts` via `import.meta.glob`; this module
 * only declares the route descriptor, so adding the screen touches ONLY this
 * feature's files (never routes/router.tsx). The shell rail already reserves
 * /sources — registering this route makes it active.
 *
 * Lazy-loaded so the sources bundle stays out of the main app chunk until visited.
 *
 * This module exports only the `route` descriptor (no component) but holds a
 * module-scope `lazy()` for code-splitting — so the react-refresh HMR heuristic is
 * disabled here; there is no component to fast-refresh.
 */
/* eslint-disable react-refresh/only-export-components */
import { lazy, Suspense } from 'react';
import { RouteFallback } from '@/routes/RouteFallback';
import type { FeatureRoute } from '@/routes/types';

const SourcesPage = lazy(() =>
  import('./components/SourcesPage').then((m) => ({ default: m.SourcesPage })),
);

export const route: FeatureRoute = {
  path: '/sources',
  element: (
    <Suspense fallback={<RouteFallback label="sources" />}>
      <SourcesPage />
    </Suspense>
  ),
  order: 25,
};
