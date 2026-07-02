/**
 * Schedules feature nav entry (ADR-0008 §3, auto-discovered) — assembled into the
 * floating nav overlay via import.meta.glob, no shared array to edit (#237).
 */
import type { FeatureNavItem } from '@/routes/types';

export const navItem: FeatureNavItem = {
  to: '/schedules',
  label: 'Schedules',
  icon: '📅',
  order: 22,
};
