/**
 * Sources feature nav entry (ADR-0008 §3, auto-discovered) — assembled into the
 * floating nav overlay via import.meta.glob, no shared array to edit (#27).
 */
import type { FeatureNavItem } from '@/routes/types';

export const navItem: FeatureNavItem = {
  to: '/sources',
  label: 'Sources',
  icon: '🔌',
  order: 15,
};
