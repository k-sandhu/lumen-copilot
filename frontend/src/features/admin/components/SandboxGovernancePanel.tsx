/**

 * SandboxGovernancePanel — the per-tenant code-execution sandbox policy (#233, area:ui).

 *

 * A WRITE panel on the admin console: it edits the tenant's enablement and package

 * policy and narrowing limits. ADR-0029 (Proposed) enables deployment ceilings

 * by default, with atomic tenant accounting and visible provenance.

 *

 * All admin-only + tenant-scoped: a non-admin caller (403, INV-5) or expired session

 * (401, INV-4) surfaces as the shared panel error, never a blank pane. Every async

 * state is handled (frontend/AGENTS.md: "every state, not just success"): a loading

 * shimmer, an actionable read error with Retry, a saving indicator, client-side

 * dismissible success/error toast so an audited action's outcome is always visible.

 */

import { useEffect, useState } from 'react';

import { ApiError } from '@/api';

import type { SandboxPolicy, SandboxPolicyUpdate } from '@/api';

import { useSandboxPolicy, useUpdateSandboxPolicy } from '../model/queries';

import { PanelBody } from './PanelState';

/** Best human-readable message for a write error (prefers the RFC-9457 Problem). */

function describeWriteError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 403) return 'You need the admin role to change the sandbox policy.';

    if (error.status === 401) return 'Your session has expired. Sign in again.';

    if (error.status === 422)
      return 'One of the policy values is invalid or exceeds the deployment ceiling.';

    return error.displayMessage;
  }

  if (error instanceof Error) return error.message;

  return 'Could not save the sandbox policy.';
}

/** Split a newline/comma-separated textarea value into a trimmed, non-empty list. */

function toList(value: string): string[] {
  return value

    .split(/[\n,]/)

    .map((s) => s.trim())

    .filter((s) => s.length > 0);
}

/** Join a list back into a newline-separated textarea value. */

function fromList(items: string[]): string {
  return items.join('\n');
}

/** Only fields enforced by reusable sandbox sessions are editable here. */

interface FormState {
  enabled: boolean;

  allowedPackages: string;

  deniedPackages: string;

  max_runtime_s: number;

  max_memory_mb: number;

  daily_runtime_cap_s: number;

  max_concurrency: number;
}

function toForm(policy: SandboxPolicy): FormState {
  return {
    enabled: policy.enabled,

    allowedPackages: fromList(policy.allowed_packages),

    deniedPackages: fromList(policy.denied_packages),

    max_runtime_s: policy.max_runtime_s,

    max_memory_mb: policy.max_memory_mb,

    daily_runtime_cap_s: policy.daily_runtime_cap_s,

    max_concurrency: policy.max_concurrency,
  };
}

function toUpdate(form: FormState, policy: SandboxPolicy): SandboxPolicyUpdate {
  return {
    enabled: form.enabled,

    allowed_packages: toList(form.allowedPackages),

    denied_packages: toList(form.deniedPackages),

    // Model-authored code remains offline; egress values are compatibility-only.

    egress_allowed: policy.egress_allowed,

    egress_allowlist: policy.egress_allowlist,

    max_runtime_s: form.max_runtime_s,

    max_memory_mb: form.max_memory_mb,

    daily_runtime_cap_s: form.daily_runtime_cap_s,

    max_concurrency: form.max_concurrency,
  };
}

/** A small accessible on/off switch mirroring the tool-governance toggle. */

function ToggleSwitch({
  checked,

  disabled,

  label,

  onToggle,
}: {
  checked: boolean;

  disabled: boolean;

  label: string;

  onToggle: () => void;
}) {
  return (
    <label className="inline-flex cursor-pointer items-center gap-2">
      <input
        type="checkbox"
        role="switch"
        checked={checked}
        disabled={disabled}
        aria-label={label}
        onChange={onToggle}
        className="h-4 w-4 accent-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50"
      />

      <span className="text-xs font-medium text-foreground-muted">{checked ? 'On' : 'Off'}</span>
    </label>
  );
}

function SandboxPolicyForm({ policy }: { policy: SandboxPolicy }) {
  const mutation = useUpdateSandboxPolicy();

  const [form, setForm] = useState<FormState>(() => toForm(policy));

  const [toast, setToast] = useState<{ kind: 'ok' | 'error'; message: string } | null>(null);

  // Re-seed the form whenever the authoritative policy changes (e.g. after a save

  // returns the clamped effective policy) so the inputs reflect what was stored.

  useEffect(() => {
    setForm(toForm(policy));
  }, [policy]);

  const set = <K extends keyof FormState>(key: K, value: FormState[K]) => {
    setForm((prev) => ({ ...prev, [key]: value }));
  };

  const handleSave = () => {
    setToast(null);

    mutation.mutate(toUpdate(form, policy), {
      onSuccess: () => setToast({ kind: 'ok', message: 'Sandbox policy saved.' }),

      onError: (error) => setToast({ kind: 'error', message: describeWriteError(error) }),
    });
  };

  const saving = mutation.isPending;

  return (
    <div className="space-y-5 p-4">
      {toast ? (
        <div
          role={toast.kind === 'error' ? 'alert' : 'status'}
          className={
            toast.kind === 'error'
              ? 'rounded-md border border-danger/40 bg-danger/5 p-3 text-sm'
              : 'rounded-md border border-border bg-surface-muted p-3 text-sm'
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

      {policy.is_default ? (
        <p className="rounded-md border border-border bg-surface-muted p-3 text-xs text-foreground-muted">
          Code execution is disabled for this tenant by default. Enable it below and set the package
          policy; changes take effect on subsequent runs.
        </p>
      ) : null}

      {/* Enable code execution */}

      <div className="flex items-center justify-between gap-4">
        <div>
          <div className="text-sm font-medium text-foreground">Code execution</div>

          <div className="text-xs text-foreground-muted">
            Allow this tenant to run sandboxed Python. Off by default (deny-by-default).
          </div>
        </div>

        <ToggleSwitch
          checked={form.enabled}
          disabled={saving}
          label={`${form.enabled ? 'Disable' : 'Enable'} code execution`}
          onToggle={() => set('enabled', !form.enabled)}
        />
      </div>

      {/* Packages */}

      <div className="grid gap-4 sm:grid-cols-2">
        <div className="flex flex-col gap-1">
          <label htmlFor="sandbox-allowed-packages" className="text-xs font-medium text-foreground">
            Allowed packages
          </label>

          <textarea
            id="sandbox-allowed-packages"
            rows={3}
            value={form.allowedPackages}
            disabled={saving}
            onChange={(e) => set('allowedPackages', e.target.value)}
            placeholder="one per line (empty = base image only)"
            className="w-full rounded-md border border-border bg-surface px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50"
          />
        </div>

        <div className="flex flex-col gap-1">
          <label htmlFor="sandbox-denied-packages" className="text-xs font-medium text-foreground">
            Denied packages
          </label>

          <textarea
            id="sandbox-denied-packages"
            rows={3}
            value={form.deniedPackages}
            disabled={saving}
            onChange={(e) => set('deniedPackages', e.target.value)}
            placeholder="one per line (takes precedence)"
            className="w-full rounded-md border border-border bg-surface px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50"
          />
        </div>
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        {(
          [
            ['max_runtime_s', 'Max runtime (seconds)', policy.max_runtime_s_ceiling, 1],

            ['max_memory_mb', 'Max memory (MiB)', policy.max_memory_mb_ceiling, 6],

            [
              'daily_runtime_cap_s',
              'Daily runtime (seconds)',
              policy.daily_runtime_cap_s_ceiling,
              1,
            ],

            ['max_concurrency', 'Concurrent executions', policy.max_concurrency_ceiling, 1],
          ] as const
        ).map(([key, label, ceiling, minimum]) => (
          <label key={key} className="flex flex-col gap-1 text-xs text-foreground">
            {label}

            <input
              type="number"
              min={minimum}
              max={ceiling}
              step={1}
              value={form[key]}
              disabled={saving}
              onChange={(event) => set(key, Number(event.target.value))}
              className="rounded-md border border-border bg-surface px-2 py-1 text-sm"
            />

            <span className="text-foreground-muted">
              Effective: {policy[key]} ·{' '}
              {policy.limit_sources[key] === 'tenant_override'
                ? 'Tenant override'
                : 'Deployment default'}{' '}
              · ceiling {ceiling}
            </span>
          </label>
        ))}
      </div>

      <div className="space-y-1 rounded-md border border-border p-3 text-xs text-foreground-muted">
        <p>CPU: {policy.cpus ?? 'Not applied'} · deployment default</p>

        <p>Processes: {policy.pids_limit ?? 'Not applied'} · deployment default</p>

        <p>
          Scratch:{' '}
          {policy.scratch_bytes === null ? 'Not applied' : `${policy.scratch_bytes / 1048576} MiB`}{' '}
          · deployment default
        </p>

        <p>
          Output collection: {policy.output_bytes_cap / 1048576} MiB · deployment default (also
          bounded to 64 files and 256K characters per stream)
        </p>

        <p>
          Active executions: {policy.current_concurrency ?? 'unknown'} of {policy.max_concurrency}
        </p>

        <p>
          Daily runtime: {policy.daily_runtime_used_s ?? 'unknown'} of {policy.daily_runtime_cap_s}{' '}
          seconds, including active reservations · resets at UTC midnight
        </p>

        {!policy.limits_enabled ? (
          <p>
            Container bounds disabled by deployment opt-out. Deadlines, output caps and tenant
            quotas still apply.
          </p>
        ) : null}

        {!policy.accounting_available ? (
          <p>
            Accounting unavailable: new executions are refused. Retry after accounting recovers.
          </p>
        ) : null}

        {policy.denial_reasons
          .filter((reason) => reason !== 'sandbox_accounting_unavailable')
          .map((reason) => (
            <p key={reason}>
              Execution restricted:{' '}
              {reason === 'sandbox_concurrency_exceeded'
                ? 'Concurrent execution limit reached. Wait for an active execution to finish.'
                : reason === 'sandbox_daily_runtime_exceeded'
                  ? 'Daily runtime allowance exhausted. Wait for the UTC reset or review the allowance.'
                  : reason}
            </p>
          ))}

        <p>
          Reusable sessions are always offline. Limit overages destroy the environment; a subsequent
          run creates a fresh one.
        </p>
      </div>

      <div className="flex items-center gap-3">
        <button
          type="button"
          onClick={handleSave}
          disabled={saving}
          className="rounded-md border border-accent bg-accent/10 px-3 py-1.5 text-sm font-medium text-foreground hover:bg-accent/20 disabled:opacity-50"
        >
          {saving ? 'Saving…' : 'Save policy'}
        </button>
      </div>
    </div>
  );
}

export function SandboxGovernancePanel() {
  const query = useSandboxPolicy();

  return (
    <section aria-labelledby="admin-sandbox-heading" className="rounded-lg border border-border">
      <header className="border-b border-border px-4 py-3">
        <h2 id="admin-sandbox-heading" className="text-sm font-semibold text-foreground">
          Sandbox governance
        </h2>

        <p className="mt-0.5 text-xs text-foreground-muted">
          Enable reusable Python sessions and control which packages may be installed. Sessions are
          fixed offline. Effective limits and tenant usage are shown below.
        </p>
      </header>

      <PanelBody
        label="sandbox policy"
        isLoading={query.isLoading}
        error={query.error}
        isEmpty={!query.data}
        emptyMessage="No sandbox policy is available."
        onRetry={() => void query.refetch()}
        loadingRows={6}
      >
        {query.data ? <SandboxPolicyForm policy={query.data} /> : null}
      </PanelBody>
    </section>
  );
}
