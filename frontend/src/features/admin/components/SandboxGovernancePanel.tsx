/**
 * SandboxGovernancePanel — the per-tenant code-execution sandbox policy (#233, area:ui).
 *
 * A WRITE panel on the admin console: it shows and edits the tenant's sandbox policy —
 * an enable toggle, package allow/deny lists, an egress toggle + allowlist, and the
 * runtime / memory / quota caps. Saving PATCHes /admin/sandbox-policy; the backend
 * clamps every cap DOWN to the deploy-wide ceiling (a per-tenant value can only narrow)
 * and strips the cloud-metadata IP from the egress allowlist (never reachable, G4), so
 * the returned effective policy is re-seeded into the form after each save. Deny-by-
 * default: a tenant with no stored policy shows code execution OFF, with every cap at
 * the config ceiling (the reader reports `is_default`).
 *
 * All admin-only + tenant-scoped: a non-admin caller (403, INV-5) or expired session
 * (401, INV-4) surfaces as the shared panel error, never a blank pane. Every async
 * state is handled (frontend/AGENTS.md: "every state, not just success"): a loading
 * shimmer, an actionable read error with Retry, a saving indicator, client-side
 * validation of the caps (positive, within the ceiling) before the PATCH, and a
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
    if (error.status === 422) return 'One of the limits is invalid. Each must be a positive number.';
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

/** The editable form state — strings for the list textareas, numbers for the caps. */
interface FormState {
  enabled: boolean;
  allowedPackages: string;
  deniedPackages: string;
  egressAllowed: boolean;
  egressAllowlist: string;
  maxRuntimeS: number;
  maxMemoryMb: number;
  dailyRuntimeCapS: number;
  maxConcurrency: number;
}

function toForm(policy: SandboxPolicy): FormState {
  return {
    enabled: policy.enabled,
    allowedPackages: fromList(policy.allowed_packages),
    deniedPackages: fromList(policy.denied_packages),
    egressAllowed: policy.egress_allowed,
    egressAllowlist: fromList(policy.egress_allowlist),
    maxRuntimeS: policy.max_runtime_s,
    maxMemoryMb: policy.max_memory_mb,
    dailyRuntimeCapS: policy.daily_runtime_cap_s,
    maxConcurrency: policy.max_concurrency,
  };
}

function toUpdate(form: FormState): SandboxPolicyUpdate {
  return {
    enabled: form.enabled,
    allowed_packages: toList(form.allowedPackages),
    denied_packages: toList(form.deniedPackages),
    egress_allowed: form.egressAllowed,
    egress_allowlist: toList(form.egressAllowlist),
    max_runtime_s: form.maxRuntimeS,
    max_memory_mb: form.maxMemoryMb,
    daily_runtime_cap_s: form.dailyRuntimeCapS,
    max_concurrency: form.maxConcurrency,
  };
}

/** Client-side validation mirroring the server's positive-cap + ceiling rules (#233). */
function validate(form: FormState, policy: SandboxPolicy): string | null {
  const caps: Array<[string, number, number]> = [
    ['Max runtime (s)', form.maxRuntimeS, policy.max_runtime_s_ceiling],
    ['Max memory (MiB)', form.maxMemoryMb, policy.max_memory_mb_ceiling],
    ['Daily runtime cap (s)', form.dailyRuntimeCapS, policy.daily_runtime_cap_s_ceiling],
    ['Max concurrency', form.maxConcurrency, policy.max_concurrency_ceiling],
  ];
  for (const [label, value, ceiling] of caps) {
    if (!Number.isFinite(value) || !Number.isInteger(value) || value < 1) {
      return `${label} must be a whole number of at least 1.`;
    }
    if (value > ceiling) {
      return `${label} cannot exceed the deploy-wide limit of ${ceiling} (it would be clamped).`;
    }
  }
  return null;
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

/** One labelled positive-integer cap input with its deploy-wide ceiling shown. */
function CapField({
  id,
  label,
  value,
  ceiling,
  disabled,
  onChange,
}: {
  id: string;
  label: string;
  value: number;
  ceiling: number;
  disabled: boolean;
  onChange: (next: number) => void;
}) {
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="text-xs font-medium text-foreground">
        {label}
      </label>
      <input
        id={id}
        type="number"
        min={1}
        max={ceiling}
        step={1}
        value={Number.isFinite(value) ? value : ''}
        disabled={disabled}
        onChange={(e) => onChange(e.target.valueAsNumber)}
        className="w-full rounded-md border border-border bg-surface px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50"
      />
      <span className="text-xs text-foreground-muted">Deploy limit: {ceiling}</span>
    </div>
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
    const problem = validate(form, policy);
    if (problem !== null) {
      setToast({ kind: 'error', message: problem });
      return;
    }
    mutation.mutate(toUpdate(form), {
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
          Code execution is disabled for this tenant by default. Enable it below and set
          limits; changes take effect on subsequent runs.
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

      {/* Egress */}
      <div className="space-y-2">
        <div className="flex items-center justify-between gap-4">
          <div>
            <div className="text-sm font-medium text-foreground">Outbound network</div>
            <div className="text-xs text-foreground-muted">
              Off = fully isolated (no network). The cloud-metadata address is never
              reachable, even if listed.
            </div>
          </div>
          <ToggleSwitch
            checked={form.egressAllowed}
            disabled={saving}
            label={`${form.egressAllowed ? 'Disable' : 'Enable'} outbound network`}
            onToggle={() => set('egressAllowed', !form.egressAllowed)}
          />
        </div>
        <div className="flex flex-col gap-1">
          <label htmlFor="sandbox-egress-allowlist" className="text-xs font-medium text-foreground">
            Egress allowlist
          </label>
          <textarea
            id="sandbox-egress-allowlist"
            rows={3}
            value={form.egressAllowlist}
            disabled={saving || !form.egressAllowed}
            onChange={(e) => set('egressAllowlist', e.target.value)}
            placeholder="host:port per line (only used when outbound network is on)"
            className="w-full rounded-md border border-border bg-surface px-2 py-1 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50"
          />
        </div>
      </div>

      {/* Caps */}
      <div className="grid gap-4 sm:grid-cols-2">
        <CapField
          id="sandbox-max-runtime"
          label="Max runtime (s)"
          value={form.maxRuntimeS}
          ceiling={policy.max_runtime_s_ceiling}
          disabled={saving}
          onChange={(v) => set('maxRuntimeS', v)}
        />
        <CapField
          id="sandbox-max-memory"
          label="Max memory (MiB)"
          value={form.maxMemoryMb}
          ceiling={policy.max_memory_mb_ceiling}
          disabled={saving}
          onChange={(v) => set('maxMemoryMb', v)}
        />
        <CapField
          id="sandbox-daily-runtime"
          label="Daily runtime cap (s)"
          value={form.dailyRuntimeCapS}
          ceiling={policy.daily_runtime_cap_s_ceiling}
          disabled={saving}
          onChange={(v) => set('dailyRuntimeCapS', v)}
        />
        <CapField
          id="sandbox-max-concurrency"
          label="Max concurrency"
          value={form.maxConcurrency}
          ceiling={policy.max_concurrency_ceiling}
          disabled={saving}
          onChange={(v) => set('maxConcurrency', v)}
        />
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
          Set the code-execution sandbox policy for this tenant — packages, outbound
          network, runtime, and resource quotas. Limits can only narrow the deploy-wide
          ceiling; changes take effect on subsequent runs.
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
