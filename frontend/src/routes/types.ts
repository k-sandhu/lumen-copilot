/**
 * Route + nav descriptors for auto-discovery (ADR-0008 §3, issue #79).
 *
 * Each feature owns its own `route.tsx` (a {@link FeatureRoute}) and, where it
 * belongs in the nav, a `nav.ts` (a {@link FeatureNavItem}). `router.tsx` and the
 * nav assemble these via `import.meta.glob` instead of a hand-edited array — so
 * adding a screen touches ONLY that feature's files, never this shared seam.
 */
import type { ReactElement } from 'react';

/** One route a feature contributes to the router. Mirrors a React Router object. */
export interface FeatureRoute {
  /** The path pattern, e.g. `/`, `/docs/*`, `/documents`. */
  path: string;
  /** The element rendered at that path. */
  element: ReactElement;
  /**
   * Sort key for deterministic registration order. Lower sorts earlier. Defaults
   * to 0; ties break on `path` so the manifest is stable across machines/builds.
   */
  order?: number;
}

/** One entry a feature contributes to the floating nav overlay. */
export interface FeatureNavItem {
  /** Destination path (must match a contributed route). */
  to: string;
  /** Human label shown in the overlay. */
  label: string;
  /** Optional emoji/icon glyph. */
  icon?: string;
  /** Sort key for nav order (lower first); ties break on `to`. */
  order?: number;
}
