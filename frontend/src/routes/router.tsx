import { createBrowserRouter } from 'react-router-dom';
import { featureRoutes } from './discovery';

/**
 * Router — auto-assembled from each feature's own route module (ADR-0008 §3,
 * issue #79). `routes/discovery.ts` scans the per-feature route modules via
 * `import.meta.glob` and yields a deterministically-ordered manifest; there is no
 * hand-edited route array here, so adding a screen touches only the owning
 * feature's files — retiring the `router.tsx` append-target.
 *
 * The chat shell (`App`) owns `/`; the standalone pages (`/docs/*`, `/features`,
 * `/documents`) are SEPARATE top-level routes (not nested) and lazy-load their
 * elements (see each feature's `route.tsx`) so their bundles stay out of the
 * main app chunk until visited.
 */
export const router = createBrowserRouter(
  featureRoutes.map(({ path, element }) => ({ path, element })),
);
