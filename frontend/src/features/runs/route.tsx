/**
 * Runs feature route (ADR-0008 §3, auto-discovered) — `/runs/*`, the run inbox +
 * run-detail surface (#237, ADR-0015). The run-history UI belongs to the same
 * logical slice as schedules (`features/schedules/`); this thin module exists only
 * because route auto-discovery scans ONE `route.tsx` per feature dir and `/runs`
 * is a distinct top-level path from `/schedules`. It renders the schedules slice's
 * RunsPage via that slice's PUBLIC barrel (no cross-feature deep import).
 *
 * Lazy-loaded so the runs bundle stays out of the main app chunk until visited.
 * This module exports only the `route` descriptor (no component) but holds a
 * module-scope `lazy()` — so the react-refresh HMR heuristic is disabled here.
 */
/* eslint-disable react-refresh/only-export-components */
import { lazy, Suspense } from 'react';
import { RouteFallback } from '@/routes/RouteFallback';
import type { FeatureRoute } from '@/routes/types';

const RunsPage = lazy(() =>
  import('@/features/schedules').then((m) => ({ default: m.RunsPage })),
);

export const route: FeatureRoute = {
  path: '/runs/*',
  element: (
    <Suspense fallback={<RouteFallback label="run history" />}>
      <RunsPage />
    </Suspense>
  ),
  order: 23,
};
