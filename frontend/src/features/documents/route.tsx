/**
 * Documents feature route (ADR-0008 §3, issue #79) — `/documents`, the
 * collections + documents management view (#49). Auth-gated inside the shell.
 * Lazy-loaded so the documents bundle stays out of the main app chunk until
 * visited (unchanged behavior). The shell chrome lives in
 * `routes/DocumentsRoute.tsx`; this module only declares the route descriptor.
 *
 * This module exports only the `route` descriptor (no component), but holds a
 * module-scope `lazy()` for code-splitting — so the react-refresh HMR heuristic
 * is disabled here; there is no component to fast-refresh.
 */
/* eslint-disable react-refresh/only-export-components */
import { lazy, Suspense } from 'react';
import { RouteFallback } from '@/routes/RouteFallback';
import type { FeatureRoute } from '@/routes/types';

const DocumentsShell = lazy(() =>
  import('@/routes/DocumentsRoute').then((m) => ({ default: m.DocumentsRoute })),
);

export const route: FeatureRoute = {
  path: '/documents',
  element: (
    <Suspense fallback={<RouteFallback label="documents" />}>
      <DocumentsShell />
    </Suspense>
  ),
  order: 20,
};
