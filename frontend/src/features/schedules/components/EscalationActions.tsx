/**
 * EscalationActions (E7-5, #239) — the human handoff on an **escalated** run's
 * detail. An escalated run is not silently dropped: the owner resumes it
 * (re-enqueue from the escalation point), cancels it (acknowledge + close), or
 * reroutes it to another owner (reassign the run's execution principal, re-enqueue
 * as them — INV-2, never widening access).
 *
 * Server-state via the slice mutation hooks (the ONLY backend caller is `@/api`).
 * All three async states are surfaced (idle / pending / error) and the controls are
 * disabled while any action is in flight, so a double-click can't fire two handoffs.
 * A failed action shows its problem message inline (a 409 if the run is no longer
 * escalated — e.g. someone already acted — a 404 if it moved out of view). Rendered
 * only for a run whose status is `escalated`.
 */
import { useState } from 'react';
import type { Run } from '@/api';
import { ApiError } from '@/api';
import { Icon } from '@/ui';
import { useCancelRun, useRerouteRun, useResumeRun } from '../model/queries';

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function actionError(error: unknown): string | null {
  if (!error) return null;
  if (error instanceof ApiError) {
    if (error.status === 409) return 'This run is no longer awaiting a decision.';
    if (error.status === 404) return 'This run is no longer available.';
    return error.problem?.detail ?? error.problem?.title ?? 'The action failed.';
  }
  return 'The action failed.';
}

export function EscalationActions({ run }: { run: Run }) {
  const resume = useResumeRun();
  const cancel = useCancelRun();
  const reroute = useRerouteRun(run.id);
  const [rerouteOpen, setRerouteOpen] = useState(false);
  const [toOwnerId, setToOwnerId] = useState('');

  const busy = resume.isPending || cancel.isPending || reroute.isPending;
  const targetValid = UUID_RE.test(toOwnerId.trim());
  const error =
    actionError(resume.error) ?? actionError(cancel.error) ?? actionError(reroute.error);

  return (
    <div className="space-y-3 border-t border-warn/30 pt-3">
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => resume.mutate(run.id)}
          disabled={busy}
          className="inline-flex items-center gap-1.5 rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-white hover:opacity-90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60"
        >
          <Icon name="corner-down-left" className="shrink-0" />
          {resume.isPending ? 'Resuming…' : 'Resume'}
        </button>

        <button
          type="button"
          onClick={() => setRerouteOpen((v) => !v)}
          disabled={busy}
          aria-expanded={rerouteOpen}
          className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-sm font-medium hover:bg-surface-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60"
        >
          <Icon name="corner-down-left" className="shrink-0" />
          Reroute…
        </button>

        <button
          type="button"
          onClick={() => cancel.mutate(run.id)}
          disabled={busy}
          className="inline-flex items-center gap-1.5 rounded-md border border-danger/50 px-3 py-1.5 text-sm font-medium text-danger hover:bg-danger/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-danger disabled:opacity-60"
        >
          <Icon name="x" className="shrink-0" />
          {cancel.isPending ? 'Cancelling…' : 'Cancel run'}
        </button>
      </div>

      {rerouteOpen ? (
        <form
          className="flex flex-wrap items-end gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (targetValid) reroute.mutate({ to_owner_id: toOwnerId.trim() });
          }}
        >
          <label className="flex flex-col gap-1 text-xs">
            <span className="font-medium text-foreground-muted">Reassign to (user id)</span>
            <input
              type="text"
              value={toOwnerId}
              onChange={(e) => setToOwnerId(e.target.value)}
              placeholder="00000000-0000-0000-0000-000000000000"
              className="w-72 rounded-md border border-border bg-surface px-2 py-1 font-mono text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
              aria-invalid={toOwnerId.length > 0 && !targetValid}
            />
          </label>
          <button
            type="submit"
            disabled={busy || !targetValid}
            className="inline-flex items-center gap-1.5 rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-white hover:opacity-90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60"
          >
            {reroute.isPending ? 'Rerouting…' : 'Reroute'}
          </button>
        </form>
      ) : null}

      {error ? (
        <p role="alert" className="text-xs text-danger">
          {error}
        </p>
      ) : null}
    </div>
  );
}
