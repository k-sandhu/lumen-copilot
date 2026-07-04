/**
 * ProvidersPanel — the per-tenant LLM providers surface (foundation PR, area:ui).
 *
 * A tenant admin registers OpenAI-compatible LLM providers (name + base URL + API
 * key); the backend auto-discovers each provider's models via GET {base_url}/models
 * and records a discovery/health status. This panel lists the registered providers
 * (name, base URL, a status badge, the count of discovered models, an enable toggle),
 * shows the discovered model ids under each provider, and offers an "Add provider"
 * form plus per-row Refresh (re-run discovery) and Delete. Rotating the write-only
 * API key or retargeting the base URL is done via the same form on an existing row.
 *
 * All admin-only + tenant-scoped: a non-admin caller (403, INV-5) or expired session
 * (401, INV-4) surfaces as the shared panel error, never a blank pane. Every async
 * state is handled (frontend/AGENTS.md: "every state, not just success"): loading
 * shimmer, an actionable read error with Retry, an empty state, a per-row saving
 * indicator, and a dismissible success/error toast so an audited action's outcome is
 * always visible. The API key is WRITE-ONLY — the form field is a password input and
 * the response never carries the value (only a masked secret_hint).
 *
 * Scope: this is the model-catalog foundation. The discovered models are NOT yet
 * wired into the chat model picker and no chat/embedding request is routed through a
 * registered provider — that is a separate follow-up PR.
 */
import { useState } from 'react';
import { Icon, StatusDot, type StatusTone } from '@/ui';
import { ApiError } from '@/api';
import type { LlmProvider, LlmProviderStatus } from '@/api';
import {
  useCreateLlmProvider,
  useDeleteLlmProvider,
  useLlmProviders,
  useRefreshLlmProvider,
  useUpdateLlmProvider,
} from '../model/queries';
import { PanelBody } from './PanelState';

/** Map a discovery status to a StatusDot tone + human label. */
const STATUS_TONE: Record<LlmProviderStatus, StatusTone> = {
  ready: 'ok',
  pending: 'sync',
  error: 'danger',
};
const STATUS_LABEL: Record<LlmProviderStatus, string> = {
  ready: 'Ready',
  pending: 'Discovering…',
  error: 'Error',
};

/** Best human-readable message for a write error (prefers the RFC-9457 Problem). */
function describeWriteError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 403) return 'You need the admin role to manage LLM providers.';
    if (error.status === 401) return 'Your session has expired. Sign in again.';
    if (error.status === 404) return 'That provider no longer exists.';
    if (error.status === 422) {
      // The problem `code` distinguishes the validation cases the service raises.
      const code = error.problem?.code;
      if (code === 'base_url_scheme') return 'The base URL must be an https:// URL.';
      if (code === 'base_url_blocked') return 'That base URL is not reachable (blocked address).';
      if (code === 'unsupported_provider_type') return 'That provider type is not supported.';
      return 'Please check the provider details and try again.';
    }
    return error.displayMessage;
  }
  if (error instanceof Error) return error.message;
  return 'Could not save the provider.';
}

/** A small accessible on/off switch (mirrors the tool-governance toggle). */
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

function ProviderRow({
  provider,
  busy,
  onToggleEnabled,
  onRefresh,
  onDelete,
}: {
  provider: LlmProvider;
  busy: boolean;
  onToggleEnabled: () => void;
  onRefresh: () => void;
  onDelete: () => void;
}) {
  const models = provider.discovered_models;
  return (
    <tr className="border-b border-border/60 align-top last:border-0">
      <td className="px-4 py-3">
        <div className="font-medium text-foreground">{provider.name}</div>
        <div className="mt-0.5 break-all text-xs text-foreground-muted">{provider.base_url}</div>
        {provider.secret_hint ? (
          <div className="mt-0.5 text-xs text-foreground-muted">key {provider.secret_hint}</div>
        ) : (
          <div className="mt-0.5 text-xs text-foreground-muted">no key</div>
        )}
      </td>
      <td className="px-4 py-3">
        <StatusDot tone={STATUS_TONE[provider.status]} label={STATUS_LABEL[provider.status]} />
        {provider.status === 'error' && provider.last_error ? (
          <div className="mt-1 max-w-xs break-words text-xs text-danger">{provider.last_error}</div>
        ) : null}
      </td>
      <td className="px-4 py-3">
        <div className="text-sm text-foreground">{models.length}</div>
        {models.length > 0 ? (
          <ul className="mt-1 space-y-0.5">
            {models.slice(0, 6).map((m) => (
              <li key={m.id} className="text-xs text-foreground-muted">
                {m.label ?? m.id}
              </li>
            ))}
            {models.length > 6 ? (
              <li className="text-xs text-foreground-muted">+{models.length - 6} more</li>
            ) : null}
          </ul>
        ) : null}
      </td>
      <td className="px-4 py-3">
        <ToggleSwitch
          checked={provider.enabled}
          disabled={busy}
          label={`${provider.enabled ? 'Disable' : 'Enable'} ${provider.name}`}
          onToggle={onToggleEnabled}
        />
      </td>
      <td className="px-4 py-3">
        <div className="flex items-center justify-end gap-2">
          <button
            type="button"
            onClick={onRefresh}
            disabled={busy}
            className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs hover:bg-surface-muted disabled:opacity-50"
          >
            <Icon name="arrow-up-right" className="h-3.5 w-3.5" aria-hidden="true" />
            Refresh
          </button>
          <button
            type="button"
            onClick={onDelete}
            disabled={busy}
            aria-label={`Delete ${provider.name}`}
            className="inline-flex items-center gap-1 rounded-md border border-danger/40 px-2 py-1 text-xs text-danger hover:bg-danger/5 disabled:opacity-50"
          >
            <Icon name="trash" className="h-3.5 w-3.5" aria-hidden="true" />
            Delete
          </button>
          {busy ? <span className="text-xs text-foreground-muted">Saving…</span> : null}
        </div>
      </td>
    </tr>
  );
}

function ProviderTable({
  providers,
  busyId,
  onToggleEnabled,
  onRefresh,
  onDelete,
}: {
  providers: LlmProvider[];
  busyId: string | null;
  onToggleEnabled: (p: LlmProvider) => void;
  onRefresh: (p: LlmProvider) => void;
  onDelete: (p: LlmProvider) => void;
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-left text-sm">
        <caption className="sr-only">Registered LLM providers</caption>
        <thead>
          <tr className="border-b border-border text-xs uppercase tracking-wide text-foreground-muted">
            <th scope="col" className="px-4 py-2 font-medium">
              Provider
            </th>
            <th scope="col" className="px-4 py-2 font-medium">
              Status
            </th>
            <th scope="col" className="px-4 py-2 font-medium">
              Models
            </th>
            <th scope="col" className="px-4 py-2 font-medium">
              Enabled
            </th>
            <th scope="col" className="px-4 py-2 font-medium text-right">
              <span className="sr-only">Actions</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {providers.map((provider) => (
            <ProviderRow
              key={provider.id}
              provider={provider}
              busy={busyId === provider.id}
              onToggleEnabled={() => onToggleEnabled(provider)}
              onRefresh={() => onRefresh(provider)}
              onDelete={() => onDelete(provider)}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function AddProviderForm({
  onSubmit,
  submitting,
}: {
  onSubmit: (body: { name: string; base_url: string; api_key?: string }) => void;
  submitting: boolean;
}) {
  const [name, setName] = useState('');
  const [baseUrl, setBaseUrl] = useState('');
  const [apiKey, setApiKey] = useState('');
  const canSubmit = name.trim().length > 0 && baseUrl.trim().length > 0 && !submitting;

  const handleSubmit = (event: React.FormEvent) => {
    event.preventDefault();
    if (!canSubmit) return;
    const body: { name: string; base_url: string; api_key?: string } = {
      name: name.trim(),
      base_url: baseUrl.trim(),
    };
    if (apiKey.trim().length > 0) body.api_key = apiKey.trim();
    onSubmit(body);
    setName('');
    setBaseUrl('');
    setApiKey('');
  };

  return (
    <form
      onSubmit={handleSubmit}
      aria-label="Add LLM provider"
      className="grid gap-3 border-t border-border bg-surface-muted/40 px-4 py-4 sm:grid-cols-2"
    >
      <label className="flex flex-col gap-1 text-xs font-medium text-foreground-muted">
        Name
        <input
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="OpenRouter"
          maxLength={200}
          className="rounded-md border border-border bg-surface px-2 py-1.5 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        />
      </label>
      <label className="flex flex-col gap-1 text-xs font-medium text-foreground-muted">
        Provider type
        {/* Only openai_compatible ships in this PR; the select is present so the
            shape is stable when more types land (the enum is small but extensible). */}
        <select
          aria-label="Provider type"
          defaultValue="openai_compatible"
          disabled
          className="rounded-md border border-border bg-surface px-2 py-1.5 text-sm text-foreground disabled:opacity-70"
        >
          <option value="openai_compatible">OpenAI-compatible</option>
        </select>
      </label>
      <label className="flex flex-col gap-1 text-xs font-medium text-foreground-muted sm:col-span-2">
        Base URL
        <input
          type="url"
          value={baseUrl}
          onChange={(e) => setBaseUrl(e.target.value)}
          placeholder="https://openrouter.ai/api/v1"
          className="rounded-md border border-border bg-surface px-2 py-1.5 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        />
      </label>
      <label className="flex flex-col gap-1 text-xs font-medium text-foreground-muted sm:col-span-2">
        API key (write-only)
        <input
          type="password"
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
          placeholder="sk-…"
          autoComplete="off"
          className="rounded-md border border-border bg-surface px-2 py-1.5 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        />
      </label>
      <div className="sm:col-span-2">
        <button
          type="submit"
          disabled={!canSubmit}
          className="inline-flex items-center gap-1 rounded-md border border-accent bg-accent/10 px-3 py-1.5 text-sm font-medium text-accent hover:bg-accent/20 disabled:opacity-50"
        >
          <Icon name="plus" className="h-4 w-4" aria-hidden="true" />
          {submitting ? 'Adding…' : 'Add provider'}
        </button>
      </div>
    </form>
  );
}

export function ProvidersPanel() {
  const query = useLlmProviders();
  const createMutation = useCreateLlmProvider();
  const updateMutation = useUpdateLlmProvider();
  const deleteMutation = useDeleteLlmProvider();
  const refreshMutation = useRefreshLlmProvider();
  const [toast, setToast] = useState<{ kind: 'ok' | 'error'; message: string } | null>(null);
  const providers = query.data?.items ?? [];

  const onOk = (message: string) => setToast({ kind: 'ok', message });
  const onError = (error: unknown) => setToast({ kind: 'error', message: describeWriteError(error) });

  const handleCreate = (body: { name: string; base_url: string; api_key?: string }) => {
    setToast(null);
    createMutation.mutate(
      { name: body.name, provider_type: 'openai_compatible', base_url: body.base_url, api_key: body.api_key },
      {
        onSuccess: (p) => onOk(`Added ${p.name} (${p.status}).`),
        onError,
      },
    );
  };

  const handleToggleEnabled = (p: LlmProvider) => {
    setToast(null);
    updateMutation.mutate(
      { id: p.id, body: { enabled: !p.enabled } },
      { onSuccess: () => onOk(`Updated ${p.name}.`), onError },
    );
  };

  const handleRefresh = (p: LlmProvider) => {
    setToast(null);
    refreshMutation.mutate(p.id, {
      onSuccess: (updated) => onOk(`Refreshed ${updated.name} (${updated.status}).`),
      onError,
    });
  };

  const handleDelete = (p: LlmProvider) => {
    setToast(null);
    deleteMutation.mutate(p.id, {
      onSuccess: () => onOk(`Removed ${p.name}.`),
      onError,
    });
  };

  // The single row currently being mutated (for the per-row saving indicator).
  const busyId =
    (updateMutation.isPending ? (updateMutation.variables?.id ?? null) : null) ??
    (refreshMutation.isPending ? (refreshMutation.variables ?? null) : null) ??
    (deleteMutation.isPending ? (deleteMutation.variables ?? null) : null);

  return (
    <section aria-labelledby="admin-providers-heading" className="rounded-lg border border-border">
      <header className="border-b border-border px-4 py-3">
        <h2 id="admin-providers-heading" className="text-sm font-semibold text-foreground">
          LLM providers
        </h2>
        <p className="mt-0.5 text-xs text-foreground-muted">
          Register OpenAI-compatible providers (OpenRouter, OpenAI, vLLM). We discover each
          provider&rsquo;s models automatically. The API key is stored write-only and never shown.
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
        label="LLM providers"
        isLoading={query.isLoading}
        error={query.error}
        isEmpty={providers.length === 0}
        emptyMessage="No LLM providers registered yet. Add one below."
        onRetry={() => void query.refetch()}
        loadingRows={4}
      >
        <ProviderTable
          providers={providers}
          busyId={busyId}
          onToggleEnabled={handleToggleEnabled}
          onRefresh={handleRefresh}
          onDelete={handleDelete}
        />
      </PanelBody>
      <AddProviderForm onSubmit={handleCreate} submitting={createMutation.isPending} />
    </section>
  );
}
