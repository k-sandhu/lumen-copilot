/**
 * Docs feature nav entry (ADR-0008 §3, issue #79). The floating nav overlay is
 * assembled from each feature's own `nav.ts` via import.meta.glob — adding a nav
 * link touches only the owning feature, never a shared array.
 *
 * Developer-only page (issue #40): when `VITE_ENABLE_DEV_PAGES` is OFF (the
 * production default) this exports `navItem: undefined`, which auto-discovery
 * filters out — so the Documentation link never appears in the nav overlay.
 */
import { DEV_PAGES_ENABLED } from '@/api';
import type { FeatureNavItem } from '@/routes/types';

export const navItem: FeatureNavItem | undefined = DEV_PAGES_ENABLED
  ? {
      to: '/docs',
      label: 'Documentation',
      icon: '📖',
      order: 20,
    }
  : undefined;
