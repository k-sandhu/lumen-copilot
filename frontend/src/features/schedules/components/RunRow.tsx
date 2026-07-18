/**
 * RunRow (#237, AC-2/AC-3) — one run in the inbox: status (dot + label), trigger,
 * the summary digest line, and start/finish times. A failed or escalated run is
 * visually flagged (a danger/amber border + a needs-attention note) so it is never
 * a silent row — the negative case (AC-3) reads at a glance. The whole row is a
 * link to the run detail.
 */
import { Link } from 'react-router-dom';
import type { Run } from '@/api';
import { StatusDot } from '@/ui';
import {
  RUN_STATUS_LABEL,
  RUN_STATUS_TONE,
  TRIGGER_LABEL,
  formatDateTime,
  isFailureOrEscalation,
} from '../model/presentation';

export function RunRow({ run, assistantName }: { run: Run; assistantName: string }) {
  const flagged = isFailureOrEscalation(run.status);
  const border = flagged
    ? run.status === 'failed'
      ? 'border-danger/50'
      : 'border-warn/50'
    : 'border-border';

  return (
    <Link
      to={`/runs/${run.id}`}
      className={`block rounded-lg border ${border} bg-surface p-4 hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent`}
      aria-label={`Run of ${assistantName} — ${RUN_STATUS_LABEL[run.status]}`}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <StatusDot tone={RUN_STATUS_TONE[run.status]} label={RUN_STATUS_LABEL[run.status]} />
          <span className="truncate text-sm font-semibold">{assistantName}</span>
          <span className="rounded-full bg-surface-muted px-2 py-0.5 text-xs text-foreground-muted">
            {TRIGGER_LABEL[run.trigger]}
          </span>
        </div>
        <span className="text-xs text-foreground-muted" title={formatDateTime(run.created_at)}>
          {formatDateTime(run.started_at ?? run.created_at)}
        </span>
      </div>

      {run.summary ? (
        <p className="mt-1.5 line-clamp-2 text-sm text-foreground-muted">{run.summary}</p>
      ) : flagged && run.error ? (
        <p className="mt-1.5 text-sm text-danger">{run.error.message}</p>
      ) : null}

      {flagged ? (
        <p
          className={`mt-1.5 text-xs font-medium ${run.status === 'failed' ? 'text-danger' : 'text-warn'}`}
        >
          {run.status === 'failed'
            ? 'This run failed — open it for the reason.'
            : 'This run needs a human — open it to review.'}
        </p>
      ) : null}
    </Link>
  );
}
