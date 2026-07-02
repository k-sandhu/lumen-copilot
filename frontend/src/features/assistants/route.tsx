/**
 * Assistants feature route (ADR-0008 §3, auto-discovered) — `/assistants/*`, the
 * assistant builder + library surface (#212, ADR-0011). Auto-discovered by
 * `routes/discovery.ts` via `import.meta.glob`; this module only declares the
 * route descriptor, so adding the screen touches ONLY this feature's files (never
 * routes/router.tsx).
 *
 * The splat `/assistants/*` lets the feature own its three product paths
 * (`/assistants`, `/assistants/new`, `/assistants/:id`) via a nested <Routes> in
 * AssistantsPage. Lazy-loaded so the assistants bundle stays out of the main app
 * chunk until visited.
 *
 * This module exports only the `route` descriptor (no component) but holds a
 * module-scope `lazy()` for code-splitting — so the react-refresh HMR heuristic is
 * disabled here; there is no component to fast-refresh.
 */
/* eslint-disable react-refresh/only-export-components */
import { lazy, Suspense } from 'react';
import { RouteFallback } from '@/routes/RouteFallback';
import type { FeatureRoute } from '@/routes/types';

const AssistantsPage = lazy(() =>
  import('./components/AssistantsPage').then((m) => ({ default: m.AssistantsPage })),
);

export const route: FeatureRoute = {
  path: '/assistants/*',
  element: (
    <Suspense fallback={<RouteFallback label="assistants" />}>
      <AssistantsPage />
    </Suspense>
  ),
  order: 20,
};
