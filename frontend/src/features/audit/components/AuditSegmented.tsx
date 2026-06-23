/**
 * AuditSegmented (#121) — a client-side segmented filter over the page of
 * events (All / Retrieval / Answer / Action / Access denied), mirroring the
 * wireframe's `.segmented` chip row. It narrows what's ALREADY fetched (no
 * refetch); the server-side event-type select in `AuditFilters` is the
 * complementary across-pages filter.
 *
 * A11y: a labelled group of `aria-pressed` toggle buttons (the kit's
 * ModeToggle / chip pattern); exactly one is pressed at a time.
 */
import { cn } from '@/lib/cn';
import type { AuditSegment } from '../model/metrics';

interface SegmentDef {
  value: AuditSegment;
  label: string;
}

const SEGMENTS: SegmentDef[] = [
  { value: 'all', label: 'All' },
  { value: 'retrieval', label: 'Retrieval' },
  { value: 'answer', label: 'Answer' },
  { value: 'action', label: 'Action' },
  { value: 'access', label: 'Access denied' },
];

interface AuditSegmentedProps {
  value: AuditSegment;
  onChange: (segment: AuditSegment) => void;
  /** Per-segment counts for the current page, shown as a subtle affordance. */
  counts: Record<AuditSegment, number>;
}

export function AuditSegmented({ value, onChange, counts }: AuditSegmentedProps) {
  return (
    <div
      role="group"
      aria-label="Filter events by type"
      className="inline-flex flex-wrap items-center gap-1 rounded-lg border border-border bg-surface p-1"
    >
      {SEGMENTS.map((seg) => {
        const active = value === seg.value;
        const count = counts[seg.value];
        return (
          <button
            key={seg.value}
            type="button"
            aria-pressed={active}
            onClick={() => onChange(seg.value)}
            className={cn(
              'inline-flex items-center gap-1.5 rounded-md px-3 py-1 text-sm font-medium transition-colors',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
              active
                ? 'bg-accent text-white'
                : 'text-foreground-muted hover:bg-surface-muted hover:text-foreground',
            )}
          >
            {seg.label}
            <span
              className={cn(
                'tabular-nums text-xs',
                active ? 'text-white/80' : 'text-foreground-muted',
              )}
            >
              {count}
            </span>
          </button>
        );
      })}
    </div>
  );
}
