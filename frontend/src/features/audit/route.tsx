/**
 * Audit feature route (ADR-0008 §3, auto-discovered) — `/audit`, the audit-log
 * screen + provenance drawer (#86). Lazy-loaded so the audit bundle stays out of
 * the main app chunk until visited, matching the other standalone pages.
 *
 * This module exports only the `route` descriptor (no component) but holds a
 * module-scope `lazy()` for code-splitting — so the react-refresh HMR heuristic
 * is disabled here; there is no component to fast-refresh.
 */
/* eslint-disable react-refresh/only-export-components */
import { lazy, Suspense } from 'react';
import { RouteFallback } from '@/routes/RouteFallback';
import type { FeatureRoute } from '@/routes/types';

const AuditPage = lazy(() => import('./index').then((m) => ({ default: m.AuditPage })));

export const route: FeatureRoute = {
  path: '/audit',
  element: (
    <Suspense fallback={<RouteFallback label="audit log" />}>
      <AuditPage />
    </Suspense>
  ),
  order: 40,
};
