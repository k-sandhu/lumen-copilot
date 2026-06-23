/**
 * Documents feature nav entry (ADR-0008 §3, issue #79). Assembled into the
 * floating nav overlay via import.meta.glob — no shared array to edit.
 */
import type { FeatureNavItem } from '@/routes/types';

export const navItem: FeatureNavItem = {
  to: '/documents',
  label: 'Documents',
  icon: '📁',
  order: 10,
};
