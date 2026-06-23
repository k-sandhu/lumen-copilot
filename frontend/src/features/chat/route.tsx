/**
 * Chat feature route (ADR-0008 §3, issue #79).
 *
 * The chat workspace owns `/` — the app shell. The shell component lives in
 * `routes/App.tsx` (it composes header + chat + the discovered nav); this module
 * only declares the route descriptor so `routes/discovery.ts` can auto-assemble
 * the router. `order: 0` keeps the index route first.
 *
 * Object-only export (no component declaration here) — nothing for the
 * react-refresh HMR heuristic to flag.
 */
import { App } from '@/routes/App';
import type { FeatureRoute } from '@/routes/types';

export const route: FeatureRoute = {
  path: '/',
  element: <App />,
  order: 0,
};
