/**
 * Settings feature route (ADR-0008 §3) — `/settings`, the user settings page: an
 * expanded version of the top-right account popover where a user sets their default
 * model, custom instructions, and profile avatar. Auto-discovered by
 * `routes/discovery.ts` via import.meta.glob, so adding this screen touches ONLY this
 * feature's files — never the shared `routes/router.tsx`. Lazy-loaded so the settings
 * bundle stays out of the main app chunk until the screen is visited.
 *
 * This module exports only the `route` descriptor (no component), but holds a
 * module-scope `lazy()` for code-splitting — so the react-refresh HMR heuristic is
 * disabled here; there is no component to fast-refresh.
 */
/* eslint-disable react-refresh/only-export-components */
import { lazy, Suspense } from 'react';
import { RouteFallback } from '@/routes/RouteFallback';
import type { FeatureRoute } from '@/routes/types';

const SettingsPage = lazy(() =>
  import('./index').then((m) => ({ default: m.SettingsPage })),
);

export const route: FeatureRoute = {
  path: '/settings',
  element: (
    <Suspense fallback={<RouteFallback label="settings" />}>
      <SettingsPage />
    </Suspense>
  ),
  order: 35,
};
