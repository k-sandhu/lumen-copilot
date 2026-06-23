/**
 * KpiCard — a single headline metric with an optional trend delta
 * (DESIGN.md §6 "KPI cards"). Supports loading (skeleton) and empty (em-dash)
 * states so an admin/audit dashboard never shows a blank tile while data is in
 * flight or absent.
 */
import { cn } from '@/lib/cn';
import { Icon } from './Icon';

export type KpiTrend = 'up' | 'down' | 'flat';

interface KpiCardProps {
  /** The metric name, e.g. "Retrievals today". */
  label: string;
  /** The headline value. Already-formatted (e.g. "1,204" or "98%"). */
  value?: string | number;
  /** Optional trend delta text, e.g. "+12% WoW". */
  delta?: string;
  /** Direction of the delta — drives color + arrow. */
  trend?: KpiTrend;
  /** Render a skeleton placeholder instead of a value. */
  loading?: boolean;
  className?: string;
}

const TREND: Record<
  KpiTrend,
  { modifier: string; icon: 'trending-up' | 'trending-down' | 'minus' }
> = {
  up: { modifier: 'lc-kpi__delta--up', icon: 'trending-up' },
  down: { modifier: 'lc-kpi__delta--down', icon: 'trending-down' },
  flat: { modifier: 'lc-kpi__delta--flat', icon: 'minus' },
};

export function KpiCard({
  label,
  value,
  delta,
  trend = 'flat',
  loading = false,
  className,
}: KpiCardProps) {
  const t = TREND[trend];
  const isEmpty = value === undefined || value === null || value === '';
  return (
    <div className={cn('lc-kpi', className)} role="group" aria-label={label}>
      <div className="lc-kpi__k">{label}</div>
      {loading ? (
        <div
          className="lc-skeleton"
          style={{
            height: 'calc(28px * var(--fs))',
            width: '60%',
            marginTop: 'calc(6px * var(--space))',
          }}
          aria-hidden="true"
        />
      ) : (
        <div className="lc-kpi__v" aria-live="polite">
          {isEmpty ? '—' : value}
        </div>
      )}
      {!loading && delta ? (
        <div className={cn('lc-kpi__delta', t.modifier)}>
          <Icon name={t.icon} />
          {delta}
        </div>
      ) : null}
    </div>
  );
}
