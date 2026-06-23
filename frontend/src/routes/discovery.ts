/**
 * Route + nav auto-discovery (ADR-0008 §3, issue #79).
 *
 * Assembles the router and the nav overlay by SCANNING the feature slices via
 * `import.meta.glob` — each feature owns its own `features/<x>/route.tsx`
 * (exporting `route: FeatureRoute`) and, where it belongs in the nav, a
 * `features/<x>/nav.ts` (exporting `navItem: FeatureNavItem`). There is no
 * hand-edited route/nav array, so adding a screen touches ONLY that feature's
 * files — retiring the `routes/router.tsx` append-target (ADR-0008 §3 table).
 *
 * Glob is `eager` so the manifests resolve synchronously at module load (the
 * route *elements* can still be lazy — see each feature's `route.tsx`), and the
 * results are sorted by `order` then `path`/`to` for deterministic registration
 * across machines and builds.
 */
import type { FeatureNavItem, FeatureRoute } from './types';

interface RouteModule {
  route: FeatureRoute;
}

interface NavModule {
  navItem: FeatureNavItem;
}

function isRouteModule(mod: unknown): mod is RouteModule {
  return (
    typeof mod === 'object' &&
    mod !== null &&
    'route' in mod &&
    typeof (mod as { route: unknown }).route === 'object' &&
    (mod as { route: unknown }).route !== null
  );
}

function isNavModule(mod: unknown): mod is NavModule {
  return (
    typeof mod === 'object' &&
    mod !== null &&
    'navItem' in mod &&
    typeof (mod as { navItem: unknown }).navItem === 'object' &&
    (mod as { navItem: unknown }).navItem !== null
  );
}

// Each feature slice may export a `route.tsx` (its route) and a `nav.ts` (its
// nav entry). Both are discovered by convention — no central registration.
const routeModules = import.meta.glob<RouteModule>('@/features/*/route.tsx', {
  eager: true,
});
const navModules = import.meta.glob<NavModule>('@/features/*/nav.ts', {
  eager: true,
});

/** Stable order: ascending `order` (default 0), ties broken on the path/key. */
function byOrderThen<T extends { order?: number }>(key: (item: T) => string) {
  return (a: T, b: T): number => {
    const delta = (a.order ?? 0) - (b.order ?? 0);
    return delta !== 0 ? delta : key(a).localeCompare(key(b));
  };
}

/** Every feature route, deterministically ordered. */
export const featureRoutes: FeatureRoute[] = Object.values(routeModules)
  .filter(isRouteModule)
  .map((mod) => mod.route)
  .sort(byOrderThen((r) => r.path));

/** Every feature nav entry, deterministically ordered (for the nav overlay). */
export const featureNavItems: FeatureNavItem[] = Object.values(navModules)
  .filter(isNavModule)
  .map((mod) => mod.navItem)
  .sort(byOrderThen((n) => n.to));
