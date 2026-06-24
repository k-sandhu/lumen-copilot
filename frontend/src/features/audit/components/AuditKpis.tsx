/**
 * AuditKpis (#121) — headline KPI tiles for the audit screen, computed
 * CLIENT-SIDE from the page of events the api/ boundary already returned
 * (no extra backend call). Three tiles only: Events, Access denied, and
 * Answers cited — each honestly scoped to "this page". The wireframe's
 * "Avg latency" tile is deliberately OMITTED: the frozen `AuditEvent` carries
 * no latency field, so showing it would mean faking data (issue #121).
 *
 * Uses the design-system `KpiCard`, which renders its own skeleton while the
 * first page loads, so the row never flashes a blank tile.
 */
import { KpiCard } from '@/ui';
import type { AuditMetrics } from '../model/metrics';
import { formatRate } from '../model/metrics';

interface AuditKpisProps {
  metrics: AuditMetrics;
  /** Skeleton tiles while the first page is in flight. */
  loading?: boolean;
}

export function AuditKpis({ metrics, loading = false }: AuditKpisProps) {
  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-3" aria-label="Audit summary">
      <KpiCard label="Events (this page)" value={loading ? undefined : metrics.total} loading={loading} />
      <KpiCard
        label="Access denied"
        value={loading ? undefined : metrics.denied}
        delta={loading ? undefined : metrics.denied === 0 ? 'none on this page' : 'this page'}
        trend={metrics.denied === 0 ? 'flat' : 'down'}
        loading={loading}
      />
      <KpiCard
        label="Answers cited"
        value={loading ? undefined : formatRate(metrics.citedRate)}
        delta={
          loading || metrics.answers === 0
            ? undefined
            : `${metrics.answersCited} of ${metrics.answers} answers`
        }
        trend="up"
        loading={loading}
      />
    </div>
  );
}
