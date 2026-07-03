/**
 * Artifacts feature nav entry (ADR-0008 §3, #222). Assembled into the floating nav
 * overlay via import.meta.glob — no shared array to edit.
 */
import type { FeatureNavItem } from '@/routes/types';

export const navItem: FeatureNavItem = {
  to: '/artifacts',
  label: 'Artifacts',
  icon: '📦',
  order: 15,
};
