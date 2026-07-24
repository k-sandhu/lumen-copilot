/**
 * Chat feature route (ADR-0008 §3, issue #79).
 *
 * The chat workspace owns `/` — the app shell. The shell component lives in
 * `routes/App.tsx` (it composes header + chat + the discovered nav); this module
 * only declares the route descriptor so `routes/discovery.ts` can auto-assemble
 * the router. `order: 0` keeps the index route first.
 *
 * Lazy-loaded (#494): the chat workspace pulls in the markdown + highlight.js
 * pipeline (react-markdown, remark-gfm, rehype-highlight, rehype-sanitize). The
 * app shell/chrome lives in the LAYOUT route (RouteGuard > AppShell), so deferring
 * this element keeps that whole pipeline OUT of the entry chunk — every other
 * feature route is already lazy the same way. The Suspense fallback shows only on
 * the first paint of `/` before the chat chunk resolves.
 *
 * This module exports only the `route` descriptor (no component), but holds a
 * module-scope `lazy()` for code-splitting, so the react-refresh HMR heuristic is
 * disabled here (nothing to fast-refresh).
 */
/* eslint-disable react-refresh/only-export-components */
import { lazy, Suspense } from 'react';
import { RouteFallback } from '@/routes/RouteFallback';
import type { FeatureRoute } from '@/routes/types';

const App = lazy(() => import('@/routes/App').then((m) => ({ default: m.App })));

export const route: FeatureRoute = {
  path: '/',
  element: (
    <Suspense fallback={<RouteFallback label="chat" />}>
      <App />
    </Suspense>
  ),
  order: 0,
};
