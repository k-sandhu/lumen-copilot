/**
 * FreshnessPill — recency at a glance (DESIGN.md §6, mission "freshness").
 * Renders a clock + a relative label; the `stale` variant warns when content is
 * past its freshness window so an answer's evidence age is never hidden.
 */
import { cn } from '@/lib/cn';
import { Icon } from './Icon';

interface FreshnessPillProps {
  /** Human-readable recency, e.g. "Indexed 2h ago" or "Updated yesterday". */
  label: string;
  /** Mark as past-freshness-window (amber). */
  stale?: boolean;
  /** Machine timestamp for assistive tech / hover, e.g. an ISO string. */
  title?: string;
  className?: string;
}

export function FreshnessPill({ label, stale = false, title, className }: FreshnessPillProps) {
  return (
    <span
      className={cn('lc-fresh', stale && 'lc-fresh--stale', className)}
      title={title}
      aria-label={title ? `${label} (${title})` : label}
    >
      <Icon name="clock" />
      {label}
    </span>
  );
}
