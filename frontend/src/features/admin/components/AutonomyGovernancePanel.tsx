/**
 * AutonomyGovernancePanel — the per-tenant assistant autonomy cap (#218, area:ui).
 *
 * A governance WRITE panel on the admin console: it shows the tenant's autonomy
 * ceiling — the maximum autonomy any assistant may EFFECTIVELY run at — as a select
 * bound to the four levels (suggest … act_auto). Choosing a level PATCHes
 * /admin/autonomy-policy (the mutation), the cap the backend's publish path and
 * run-time tool gate consult: an assistant configured above the cap is rejected at
 * publish and clamped at run time (an effective autonomy = min(configured, cap)).
 * Admin-only + tenant-scoped: a non-admin caller (403, INV-5) or expired session
 * (401, INV-4) surfaces as the shared panel error, never a blank pane.
 *
 * Every async state is handled (frontend/AGENTS.md: "every state, not just success"):
 * a loading shimmer, an actionable read error with Retry, a saving indicator, and a
 * dismissible success/error toast so an audited action's outcome is always visible.
 * The cap is *permissive by default* — with no stored cap the effective ceiling is
 * `act_auto` (no limit), which the panel labels so the admin understands the baseline.
 */
import { useState } from 'react';
import { ApiError } from '@/api';
import type { AutonomyLevel } from '@/api';
import { useAutonomyPolicy, useUpdateAutonomyPolicy } from '../model/queries';
import { PanelBody } from './PanelState';

/** Human labels + a one-line meaning for each autonomy level (ADR-0011 §3). */
const AUTONOMY_LABELS: Record<AutonomyLevel, { label: string; hint: string }> = {
  suggest: { label: 'Suggest', hint: 'Proposes answers only — never acts.' },
  draft: { label: 'Draft', hint: 'Drafts content for a human to send.' },
  act_with_approval: {
    label: 'Act with approval',
    hint: 'May take actions, each gated on explicit approval.',
  },
  act_auto: {
    label: 'Act automatically',
    hint: 'Acts without per-action approval (capped by tool risk tier).',
  },
};

function autonomyLabel(level: AutonomyLevel): string {
  return AUTONOMY_LABELS[level]?.label ?? level;
}

/** Best human-readable message for a write error (prefers the RFC-9457 Problem). */
function describeWriteError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 403) return 'You need the admin role to change the autonomy cap.';
    if (error.status === 401) return 'Your session has expired. Sign in again.';
    if (error.status === 422) return 'That autonomy level is not recognised.';
    return error.displayMessage;
  }
  if (error instanceof Error) return error.message;
  return 'Could not save the autonomy cap.';
}

export function AutonomyGovernancePanel() {
  const query = useAutonomyPolicy();
  const mutation = useUpdateAutonomyPolicy();
  const [toast, setToast] = useState<{ kind: 'ok' | 'error'; message: string } | null>(null);
  const policy = query.data;
  // Order the select the same ascending way the contract lists them; fall back to the
  // fixed order if the server list is somehow empty (never render an empty select).
  const levels: AutonomyLevel[] =
    policy?.levels && policy.levels.length > 0
      ? policy.levels
      : ['suggest', 'draft', 'act_with_approval', 'act_auto'];

  const handleChange = (next: AutonomyLevel) => {
    setToast(null);
    mutation.mutate(
      { max_autonomy: next },
      {
        onSuccess: () =>
          setToast({ kind: 'ok', message: `Autonomy cap set to ${autonomyLabel(next)}.` }),
        onError: (error) => setToast({ kind: 'error', message: describeWriteError(error) }),
      },
    );
  };

  return (
    <section aria-labelledby="admin-autonomy-heading" className="rounded-lg border border-border">
      <header className="border-b border-border px-4 py-3">
        <h2 id="admin-autonomy-heading" className="text-sm font-semibold text-foreground">
          Autonomy cap
        </h2>
        <p className="mt-0.5 text-xs text-foreground-muted">
          The maximum autonomy any assistant in this tenant may run at. An assistant
          configured above the cap is rejected at publish and clamped at run time — its
          effective autonomy is the lower of its own level and this ceiling.
        </p>
      </header>
      {toast ? (
        <div
          role={toast.kind === 'error' ? 'alert' : 'status'}
          className={
            toast.kind === 'error'
              ? 'mx-4 mt-3 rounded-md border border-danger/40 bg-danger/5 p-3 text-sm'
              : 'mx-4 mt-3 rounded-md border border-border bg-surface-muted p-3 text-sm'
          }
        >
          <div className="flex items-center justify-between gap-3">
            <span className="text-foreground">{toast.message}</span>
            <button
              type="button"
              onClick={() => setToast(null)}
              className="rounded-md border border-border px-2 py-0.5 text-xs hover:bg-surface"
            >
              Dismiss
            </button>
          </div>
        </div>
      ) : null}
      <PanelBody
        label="autonomy cap"
        isLoading={query.isLoading}
        error={query.error}
        isEmpty={false}
        emptyMessage="No autonomy policy."
        onRetry={() => void query.refetch()}
        loadingRows={2}
      >
        {policy ? (
          <div className="space-y-3 p-4">
            <label
              htmlFor="admin-autonomy-cap"
              className="block text-sm font-medium text-foreground"
            >
              Maximum autonomy
            </label>
            <select
              id="admin-autonomy-cap"
              value={policy.max_autonomy}
              disabled={mutation.isPending}
              onChange={(e) => handleChange(e.target.value as AutonomyLevel)}
              className="w-full max-w-sm rounded-md border border-border bg-surface px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60"
            >
              {levels.map((level) => (
                <option key={level} value={level}>
                  {autonomyLabel(level)}
                </option>
              ))}
            </select>
            <p className="text-xs text-foreground-muted">
              {AUTONOMY_LABELS[policy.max_autonomy]?.hint}
            </p>
            <p className="text-xs text-foreground-muted">
              {policy.is_default
                ? 'No cap is set — assistants run at their own configured level (no ceiling).'
                : 'A cap is set — assistants above it are clamped down to this level.'}
              {mutation.isPending ? ' Saving…' : ''}
            </p>
          </div>
        ) : null}
      </PanelBody>
    </section>
  );
}
