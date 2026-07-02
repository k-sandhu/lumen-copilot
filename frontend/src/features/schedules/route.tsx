/**
 * Schedules feature route (ADR-0008 §3, auto-discovered) — `/schedules/*`, the
 * schedule management surface (#237, ADR-0015). Auto-discovered by
 * `routes/discovery.ts` via `import.meta.glob`; this module only declares the
 * route descriptor, so adding the screen touches ONLY this feature's files (never
 * routes/router.tsx).
 *
 * The splat `/schedules/*` lets the feature own its product paths (`/schedules`,
 * `/schedules/new`, `/schedules/:id`) via a nested <Routes> in SchedulesPage.
 * Lazy-loaded so the schedules bundle stays out of the main app chunk until
 * visited. The sibling `/runs/*` surface is a second discovered route module
 * (features/runs/route.tsx) that renders this slice's RunsPage.
 *
 * This module exports only the `route` descriptor (no component) but holds a
 * module-scope `lazy()` for code-splitting — so the react-refresh HMR heuristic is
 * disabled here; there is no component to fast-refresh.
 */
/* eslint-disable react-refresh/only-export-components */
import { lazy, Suspense } from 'react';
import { RouteFallback } from '@/routes/RouteFallback';
import type { FeatureRoute } from '@/routes/types';

const SchedulesPage = lazy(() =>
  import('./components/SchedulesPage').then((m) => ({ default: m.SchedulesPage })),
);

export const route: FeatureRoute = {
  path: '/schedules/*',
  element: (
    <Suspense fallback={<RouteFallback label="schedules" />}>
      <SchedulesPage />
    </Suspense>
  ),
  order: 22,
};
