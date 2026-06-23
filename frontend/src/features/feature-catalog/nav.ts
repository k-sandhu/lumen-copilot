/**
 * Feature-catalog nav entry (ADR-0008 §3, issue #79). Assembled into the floating
 * nav overlay via import.meta.glob — no shared array to edit.
 *
 * Developer-only page (issue #40): when `VITE_ENABLE_DEV_PAGES` is OFF (the
 * production default) this exports `navItem: undefined`, which auto-discovery
 * filters out — so the "Features built" link never appears in the nav overlay.
 */
import type { FeatureNavItem } from '@/routes/types';

// Gated on the same `define`-injected build-time literal as the route
// (`__DEV_PAGES_ENABLED__`, vite.config.ts from VITE_ENABLE_DEV_PAGES) — issue
// #40. OFF (the production default) ⇒ `undefined`, which auto-discovery drops,
// so the "Features built" link never appears in the nav overlay and the branch
// is dead-code-eliminated.
export const navItem: FeatureNavItem | undefined = __DEV_PAGES_ENABLED__
  ? {
      to: '/features',
      label: 'Features built',
      icon: '✨',
      order: 30,
    }
  : undefined;
