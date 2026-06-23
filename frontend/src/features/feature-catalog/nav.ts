/**
 * Feature-catalog nav entry (ADR-0008 §3, issue #79). Assembled into the floating
 * nav overlay via import.meta.glob — no shared array to edit.
 *
 * Developer-only page (issue #40): when `VITE_ENABLE_DEV_PAGES` is OFF (the
 * production default) this exports `navItem: undefined`, which auto-discovery
 * filters out — so the "Features built" link never appears in the nav overlay.
 */
import { DEV_PAGES_ENABLED } from '@/api';
import type { FeatureNavItem } from '@/routes/types';

export const navItem: FeatureNavItem | undefined = DEV_PAGES_ENABLED
  ? {
      to: '/features',
      label: 'Features built',
      icon: '✨',
      order: 30,
    }
  : undefined;
