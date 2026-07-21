/**
 * DeliveryRow (#238) — one in-app run delivery in the inbox: status (dot + label),
 * the summary line, when it arrived, and a link to the full run (its cited
 * transcript). An unread delivery reads as fresh; a `failed` delivery is flagged in
 * danger so it is never a silent drop (AC-3). A "Mark read" button dismisses it from
 * the unread badge; opening the run marks it read too.
 */
import { Link } from 'react-router-dom';
import type { RunDelivery } from '@/api';
import { StatusDot } from '@/ui';
import {
  DELIVERY_STATUS_LABEL,
  DELIVERY_STATUS_TONE,
  formatDateTime,
  relativeTime,
} from '../model/presentation';

export function DeliveryRow({
  delivery,
  onMarkRead,
  markingRead,
}: {
  delivery: RunDelivery;
  onMarkRead: (id: string) => void;
  markingRead: boolean;
}) {
  const unread = delivery.status !== 'read';
  const failed = delivery.status === 'failed';
  const border = failed ? 'border-danger/50' : unread ? 'border-accent/40' : 'border-border';

  return (
    <div
      className={`rounded-lg border ${border} bg-surface p-4`}
      aria-label={`Run delivery — ${DELIVERY_STATUS_LABEL[delivery.status]}`}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <StatusDot
            tone={DELIVERY_STATUS_TONE[delivery.status]}
            label={DELIVERY_STATUS_LABEL[delivery.status]}
          />
          {unread ? (
            <span className="rounded-full bg-accent/15 px-2 py-0.5 text-xs font-medium text-accent">
              Unread
            </span>
          ) : null}
        </div>
        <span className="text-xs text-foreground-muted" title={formatDateTime(delivery.created_at)}>
          {relativeTime(delivery.created_at)}
        </span>
      </div>

      <p
        className={`mt-1.5 line-clamp-2 text-sm ${failed ? 'text-danger' : 'text-foreground-muted'}`}
      >
        {delivery.summary ??
          (failed
            ? 'This run’s delivery could not be produced — open the run to review and retry.'
            : 'A run completed.')}
      </p>

      <div className="mt-2 flex items-center gap-3 text-sm">
        <Link
          to={`/runs/${delivery.run_id}`}
          className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1 hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          Open run
        </Link>
        {unread ? (
          <button
            type="button"
            onClick={() => onMarkRead(delivery.id)}
            disabled={markingRead}
            className="rounded-md px-2 py-1 text-foreground-muted hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60"
          >
            {markingRead ? 'Marking…' : 'Mark read'}
          </button>
        ) : null}
      </div>
    </div>
  );
}
