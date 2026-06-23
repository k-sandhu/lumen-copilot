/**
 * Admin feature route (ADR-0008 §3, issue #79) — `/admin`, the read-only admin
 * console (#88). Auto-discovered by `routes/discovery.ts` via import.meta.glob, so
 * adding this screen touches ONLY this feature's files — never the shared
 * `routes/router.tsx`. Lazy-loaded so the admin bundle (tables + the trust kit)
 * stays out of the main app chunk until the screen is visited.
 *
 * This module exports only the `route` descriptor (no component), but holds a
 * module-scope `lazy()` for code-splitting — so the react-refresh HMR heuristic
 * is disabled here; there is no component to fast-refresh.
 */
/* eslint-disable react-refresh/only-export-components */
import { lazy, Suspense } from 'react';
import { RouteFallback } from '@/routes/RouteFallback';
import type { FeatureRoute } from '@/routes/types';

const AdminPage = lazy(() => import('./index').then((m) => ({ default: m.AdminPage })));

export const route: FeatureRoute = {
  path: '/admin',
  element: (
    <Suspense fallback={<RouteFallback label="admin console" />}>
      <AdminPage />
    </Suspense>
  ),
  order: 30,
};
