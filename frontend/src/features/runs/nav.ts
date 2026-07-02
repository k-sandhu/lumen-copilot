/**
 * Runs feature nav entry (ADR-0008 §3, auto-discovered) — the run inbox link in
 * the floating nav overlay (#237). Sits next to Schedules.
 */
import type { FeatureNavItem } from '@/routes/types';

export const navItem: FeatureNavItem = {
  to: '/runs',
  label: 'Run history',
  icon: '🗂️',
  order: 23,
};
