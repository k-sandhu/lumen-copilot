/**
 * Admin feature nav entry (ADR-0008 §3, issue #79). Assembled into the floating
 * nav overlay via import.meta.glob — adding this link touches only the admin
 * slice, never a shared array.
 */
import type { FeatureNavItem } from '@/routes/types';

export const navItem: FeatureNavItem = {
  to: '/admin',
  label: 'Admin',
  icon: '🛡️',
  order: 40,
};
