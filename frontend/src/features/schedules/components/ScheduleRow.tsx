/**
 * ScheduleRow (#237, AC-1) — one schedule in the list: assistant, cadence,
 * timezone, next/last run, last status, an enabled toggle, and the pause/resume +
 * run-now + edit actions wired to the contract's POST endpoints. Presentational:
 * it takes the schedule + callbacks and renders; the panel owns the mutations.
 *
 * A11y: the enabled toggle is a labelled `switch`; every action is a real button
 * with an accessible name; the status is announced via the kit StatusDot.
 */
import { Link } from 'react-router-dom';
import type { Schedule } from '@/api';
import { Icon, StatusDot } from '@/ui';
import { describeCadence } from '../model/cadence';
import {
  formatDateTime,
  lastStatusLabel,
  lastStatusTone,
  relativeTime,
} from '../model/presentation';

interface ScheduleRowProps {
  schedule: Schedule;
  assistantName: string;
  onToggleEnabled: (schedule: Schedule) => void;
  onRunNow: (schedule: Schedule) => void;
  /** Whether a pause/resume mutation is in flight for THIS schedule. */
  toggling: boolean;
  /** Whether a run-now mutation is in flight for THIS schedule. */
  running: boolean;
}

export function ScheduleRow({
  schedule,
  assistantName,
  onToggleEnabled,
  onRunNow,
  toggling,
  running,
}: ScheduleRowProps) {
  const { enabled } = schedule;
  return (
    <div className="grid grid-cols-1 gap-3 rounded-lg border border-border bg-surface p-4 md:grid-cols-[minmax(0,1fr)_auto] md:items-center">
      <div className="min-w-0 space-y-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="truncate text-sm font-semibold">{assistantName}</span>
          <StatusDot
            tone={enabled ? lastStatusTone(schedule.last_status) : 'muted'}
            label={enabled ? lastStatusLabel(schedule.last_status) : 'Paused'}
          />
        </div>
        <p className="text-sm text-foreground-muted">
          {describeCadence(schedule.cadence)} · {schedule.timezone}
        </p>
        <dl className="flex flex-wrap gap-x-6 gap-y-1 text-xs text-foreground-muted">
          <div className="flex gap-1">
            <dt className="font-medium">Next run:</dt>
            <dd title={formatDateTime(schedule.next_run_at)}>
              {enabled ? relativeTime(schedule.next_run_at) : 'Paused'}
            </dd>
          </div>
          <div className="flex gap-1">
            <dt className="font-medium">Last run:</dt>
            <dd title={formatDateTime(schedule.last_run_at)}>
              {relativeTime(schedule.last_run_at)}
            </dd>
          </div>
        </dl>
      </div>

      <div className="flex flex-wrap items-center gap-2 md:justify-end">
        <button
          type="button"
          role="switch"
          aria-checked={enabled}
          aria-label={`${enabled ? 'Pause' : 'Resume'} schedule for ${assistantName}`}
          disabled={toggling}
          onClick={() => onToggleEnabled(schedule)}
          className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-sm hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60"
        >
          <Icon name={enabled ? 'pause' : 'play'} className="shrink-0" />
          {toggling ? '…' : enabled ? 'Pause' : 'Resume'}
        </button>

        <button
          type="button"
          onClick={() => onRunNow(schedule)}
          disabled={running}
          aria-label={`Run ${assistantName} now`}
          className="inline-flex items-center gap-1.5 rounded-md border border-accent px-2.5 py-1.5 text-sm font-medium text-accent hover:bg-accent/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60"
        >
          <Icon name="play" className="shrink-0" />
          {running ? 'Starting…' : 'Run now'}
        </button>

        <Link
          to={`/schedules/${schedule.id}`}
          aria-label={`Edit schedule for ${assistantName}`}
          className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-sm hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          <Icon name="settings" className="shrink-0" />
          Edit
        </Link>
      </div>
    </div>
  );
}
