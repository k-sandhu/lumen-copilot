import { createBrowserRouter } from 'react-router-dom';
import { RouteGuard } from '@/features/auth';
import { featureRoutes } from './discovery';
import { AppShell } from './shell';

/**
 * Router — a single LAYOUT route (issue #110) wraps EVERY authenticated screen in
 * the app shell, so the brand cell + top bar + left nav rail stay in lock-step
 * across navigations. The layout is:
 *
 *   RouteGuard (#48 auth gate)  →  AppShell (chrome + <Outlet/>)  →  the screen
 *
 * The screens are the AUTO-DISCOVERED feature routes (ADR-0008 §3, issue #79):
 * `routes/discovery.ts` scans each feature's own `route.tsx` via `import.meta.glob`
 * and yields a deterministically-ordered manifest. We nest that SAME manifest as
 * the layout's `children` — there is no hand-edited flat route array here, so
 * adding a screen still touches only the owning feature's files (the `router.tsx`
 * append-target stays retired).
 *
 * The layout route is pathless; its children keep their own absolute paths (`/`,
 * `/search`, `/documents`, `/audit`, `/admin`, and the dev pages `/docs/*`,
 * `/features`). Every one of them renders inside the shell's main; the chat
 * workspace keeps its own multi-pane layout there.
 */
export const router = createBrowserRouter([
  {
    element: (
      <RouteGuard>
        <AppShell />
      </RouteGuard>
    ),
    children: featureRoutes.map(({ path, element }) => ({ path, element })),
  },
]);
