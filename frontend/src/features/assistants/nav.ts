/**
 * Assistants feature nav entry (ADR-0008 §3, auto-discovered) — assembled into the
 * floating nav overlay via import.meta.glob, no shared array to edit (#212).
 */
import type { FeatureNavItem } from '@/routes/types';

export const navItem: FeatureNavItem = {
  to: '/assistants',
  label: 'Assistants',
  icon: '✨',
  order: 12,
};
