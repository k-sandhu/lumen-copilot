/**
 * Settings feature nav entry (ADR-0008 §3). Assembled into the floating nav overlay
 * via import.meta.glob — adding this link touches only the settings slice, never a
 * shared array. Sorts just after Admin.
 */
import type { FeatureNavItem } from '@/routes/types';

export const navItem: FeatureNavItem = {
  to: '/settings',
  label: 'Settings',
  icon: '⚙️',
  order: 45,
};
