/**
 * Pure presentation mappers for the schedules + runs slice (#237) — status →
 * tone/label, timestamp formatting, and the run-inbox digest. Kept out of the JSX
 * so components stay presentational and these map exactly once (frontend/AGENTS.md).
 */
import type { StatusTone } from '@/ui';
import type { RunDeliveryStatus, RunStatus, RunTrigger } from '@/api';

/** Human label for each terminal/active run status. */
export const RUN_STATUS_LABEL: Record<RunStatus, string> = {
  queued: 'Queued',
  running: 'Running',
  succeeded: 'Succeeded',
  failed: 'Failed',
  escalated: 'Escalated',
};

/**
 * The trust-signal dot tone for a run status. `escalated` is a first-class
 * terminal that needs a human — surfaced as a WARNING (distinct from failed's
 * danger) so it reads as "needs attention", not "broke" (ADR-0015 §6).
 */
export const RUN_STATUS_TONE: Record<RunStatus, StatusTone> = {
  queued: 'muted',
  running: 'sync',
  succeeded: 'ok',
  failed: 'danger',
  escalated: 'warn',
};

/** A schedule's last-fire status may be null before its first fire. */
export function lastStatusLabel(status: RunStatus | null | undefined): string {
  return status ? RUN_STATUS_LABEL[status] : 'Never run';
}

export function lastStatusTone(status: RunStatus | null | undefined): StatusTone {
  return status ? RUN_STATUS_TONE[status] : 'muted';
}

/** Trigger label for the run inbox. */
export const TRIGGER_LABEL: Record<RunTrigger, string> = {
  schedule: 'Scheduled',
  manual: 'Manual',
};

/** Human label for each in-app run-delivery status (#238). */
export const DELIVERY_STATUS_LABEL: Record<RunDeliveryStatus, string> = {
  pending: 'Queued for digest',
  delivered: 'New',
  read: 'Read',
  failed: 'Delivery failed',
};

/**
 * The trust-signal dot tone for a delivery status. A `failed` delivery is a DANGER
 * (visible + retryable, never a silent drop — AC-3); `delivered` reads as a fresh,
 * unread item; `read` and `pending` are muted.
 */
export const DELIVERY_STATUS_TONE: Record<RunDeliveryStatus, StatusTone> = {
  pending: 'muted',
  delivered: 'ok',
  read: 'muted',
  failed: 'danger',
};

/** Whether a run is in a terminal state (drives the "watch live" affordance). */
export function isTerminal(status: RunStatus): boolean {
  return status === 'succeeded' || status === 'failed' || status === 'escalated';
}

/** Whether a run is a failure or escalation (drives the prominent surface, AC-3). */
export function isFailureOrEscalation(status: RunStatus): boolean {
  return status === 'failed' || status === 'escalated';
}

/**
 * Format an ISO timestamp as a short local datetime, or an em-dash placeholder for
 * null (never a blank cell). Locale-driven; not faked when absent.
 */
export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleString(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  });
}

/** Relative "in 3h" / "2d ago" style label for next/last run at-a-glance. */
export function relativeTime(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return '—';
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return '—';
  const past = t < now;
  const mins = Math.round(Math.abs(t - now) / 60000);
  if (mins < 1) return past ? 'just now' : 'now';

  // Step the count up through minutes → hours → days → weeks.
  const steps = [60, 24, 7] as const;
  const labels = ['min', 'h', 'd', 'w'] as const;
  let value = mins;
  let idx = 0;
  while (idx < steps.length && value >= steps[idx]!) {
    value = Math.round(value / steps[idx]!);
    idx += 1;
  }
  const unit = labels[idx] ?? 'w';
  return past ? `${value}${unit} ago` : `in ${value}${unit}`;
}
