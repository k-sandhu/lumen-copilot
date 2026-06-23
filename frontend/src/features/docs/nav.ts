/**
 * Docs feature nav entry (ADR-0008 §3, issue #79). The floating nav overlay is
 * assembled from each feature's own `nav.ts` via import.meta.glob — adding a nav
 * link touches only the owning feature, never a shared array.
 *
 * Developer-only page (issue #40): when `VITE_ENABLE_DEV_PAGES` is OFF (the
 * production default) this exports `navItem: undefined`, which auto-discovery
 * filters out — so the Documentation link never appears in the nav overlay.
 */
import type { FeatureNavItem } from '@/routes/types';

// Gated on the same `define`-injected build-time literal as the route
// (`__DEV_PAGES_ENABLED__`, vite.config.ts from VITE_ENABLE_DEV_PAGES) — issue
// #40. OFF (the production default) ⇒ `undefined`, which auto-discovery drops,
// so the Documentation link never appears in the nav overlay and the branch is
// dead-code-eliminated.
export const navItem: FeatureNavItem | undefined = __DEV_PAGES_ENABLED__
  ? {
      to: '/docs',
      label: 'Documentation',
      icon: '📖',
      order: 20,
    }
  : undefined;
