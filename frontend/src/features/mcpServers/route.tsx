/**
 * MCP-servers feature route (ADR-0008 §3, auto-discovered) — `/mcp-servers`, the
 * server grid + register/test/enable/remove + tools detail screen (#228, ADR-0012).
 * Auto-discovered by `routes/discovery.ts` via `import.meta.glob`; this module
 * only declares the route descriptor, so adding the screen touches ONLY this
 * feature's files (never routes/router.tsx).
 *
 * Lazy-loaded so the MCP bundle stays out of the main app chunk until visited.
 *
 * This module exports only the `route` descriptor (no component) but holds a
 * module-scope `lazy()` for code-splitting — so the react-refresh HMR heuristic is
 * disabled here; there is no component to fast-refresh.
 */
/* eslint-disable react-refresh/only-export-components */
import { lazy, Suspense } from 'react';
import { RouteFallback } from '@/routes/RouteFallback';
import type { FeatureRoute } from '@/routes/types';

const ServersPage = lazy(() =>
  import('./components/ServersPage').then((m) => ({ default: m.ServersPage })),
);

export const route: FeatureRoute = {
  path: '/mcp-servers',
  element: (
    <Suspense fallback={<RouteFallback label="MCP servers" />}>
      <ServersPage />
    </Suspense>
  ),
  order: 26,
};
