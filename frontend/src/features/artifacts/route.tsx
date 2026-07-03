/**
 * Artifacts feature route (ADR-0008 §3, #222) — `/artifacts`, the artifact panel
 * (list / preview / download / delete of agent-produced files). Auth-gated inside
 * the shell. Lazy-loaded so the artifacts bundle stays out of the main app chunk
 * until visited.
 *
 * This module exports only the `route` descriptor (no component), but holds a
 * module-scope `lazy()` for code-splitting — so the react-refresh HMR heuristic is
 * disabled here; there is no component to fast-refresh.
 */
/* eslint-disable react-refresh/only-export-components */
import { lazy, Suspense } from 'react';
import { RouteFallback } from '@/routes/RouteFallback';
import type { FeatureRoute } from '@/routes/types';

const ArtifactsShell = lazy(() =>
  import('@/routes/ArtifactsRoute').then((m) => ({ default: m.ArtifactsRoute })),
);

export const route: FeatureRoute = {
  path: '/artifacts',
  element: (
    <Suspense fallback={<RouteFallback label="artifacts" />}>
      <ArtifactsShell />
    </Suspense>
  ),
  order: 25,
};
