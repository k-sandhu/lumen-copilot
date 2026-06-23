/**
 * Docs feature nav entry (ADR-0008 §3, issue #79). The floating nav overlay is
 * assembled from each feature's own `nav.ts` via import.meta.glob — adding a nav
 * link touches only the owning feature, never a shared array.
 */
import type { FeatureNavItem } from '@/routes/types';

export const navItem: FeatureNavItem = {
  to: '/docs',
  label: 'Documentation',
  icon: '📖',
  order: 20,
};
