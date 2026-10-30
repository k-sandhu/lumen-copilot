/**
 * Provider registration and evidence-based validation (spec 0029, #585).
 * Catalog discovery proves reachability; the explicit bounded connection test
 * verifies chat and credential acceptance. Diagnostics use local safe messages.
 */
import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { SecretInput, type SecretInputHandle } from '@/components/SecretInput';
import { useCredentialClearer } from '@/lib/credentialLifecycle';
import { scrubCredentialInput } from '@/lib/credentialDom';
import { Icon, StatusDot, type StatusTone } from '@/ui';
import { ApiError } from '@/api';
import type {
  LlmProvider,
  LlmProviderStatus,
  ProviderValidationCheck,
  ProviderValidationCheckName,
  ProviderValidationCode,
  ProviderValidationResult,
} from '@/api';
import {
  useCreateLlmProvider,
  useDeleteLlmProvider,
  useLlmProviders,
  useRefreshLlmProvider,
  useTestLlmProviderConnection,
  useUpdateLlmProvider,
} from '../model/queries';
import { PanelBody } from './PanelState';

/** Map a validation status to a StatusDot tone + human label (spec 0029). */
const STATUS_TONE: Record<LlmProviderStatus, StatusTone> = {
  ready: 'ok',
  reachable: 'warn',
  pending: 'sync',
  error: 'danger',
};
const STATUS_LABEL: Record<LlmProviderStatus, string> = {
  ready: 'Ready',
  reachable: 'Reachable \u2014 unverified',
  pending: 'Discovering…',
  error: 'Error',
};

const CHECK_LABEL: Record<ProviderValidationCheckName, string> = {
  config_valid: 'Configuration',
  reachable: 'Reachable',
  catalog_discovered: 'Catalog discovered',
  auth_verified: 'Authentication',
  capabilities_verified: 'Chat capability',
};

const CHECK_STATUS_LABEL: Record<ProviderValidationCheck['status'], string> = {
  passed: 'passed',
  failed: 'failed',
  unverified: 'unverified',
  not_applicable: 'not applicable',
};

const VALIDATION_MESSAGE: Record<NonNullable<ProviderValidationCode>, string> = {
  blocked_address: 'The provider address is blocked by the egress policy.',
  dns_failed: 'The provider host could not be resolved.',
  timeout: 'The provider did not respond in time.',
  connection_failed: 'Could not connect to the provider.',
  unauthorized: 'The provider rejected the API key (401).',
  forbidden: 'The provider refused access with the configured key (403).',
  not_found: 'The provider endpoint or model was not found (404).',
  rate_limited: 'The provider rate-limited the request (429).',
  provider_error: 'The provider returned a server error (5xx).',
  unexpected_status: 'The provider returned an unexpected status.',
  invalid_response: 'The provider returned an unexpected response shape.',
  response_too_large: 'The provider response was too large.',
  validation_failed: 'Provider validation failed.',
};

function safeCode(code: unknown): NonNullable<ProviderValidationCode> {
  return typeof code === 'string' && Object.hasOwn(VALIDATION_MESSAGE, code)
    ? (code as NonNullable<ProviderValidationCode>)
    : 'validation_failed';
}

function validationMessage(code: unknown): string {
  const safe = safeCode(code);
  return `${VALIDATION_MESSAGE[safe]} (${safe})`;
}

function safeCorrelationId(value: string): string {
  return /^[a-zA-Z0-9_-]{1,100}$/.test(value) ? value : 'unavailable';
}

function validationDiagnostics(validation: ProviderValidationResult): string {
  const stage =
    validation.stage === 'connection_test'
      ? 'Connection test'
      : validation.stage === 'discovery'
        ? 'Discovery'
        : 'Configuration';
  return `${stage}. ${validationMessage(validation.code)} Correlation: ${safeCorrelationId(validation.correlation_id)}. ${validation.retryable ? 'Retry may succeed.' : 'Check the provider configuration before retrying.'}`;
}

/** The independent validation checks for one provider (spec 0029). */
function ValidationChecks({ checks }: { checks: ProviderValidationCheck[] }) {
  if (checks.length === 0) return null;
  return (
    <ul className="mt-1 space-y-0.5" aria-label="Validation checks">
      {checks.map((check) => (
        <li key={check.name} className="text-xs text-foreground-muted">
          {CHECK_LABEL[check.name]}: {CHECK_STATUS_LABEL[check.status]}
          {check.status === 'failed' && check.code ? ` (${safeCode(check.code)})` : null}
        </li>
      ))}
    </ul>
  );
}

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
      if (code === 'test_model_unknown') return 'Pick a model from the discovered catalog.';
      return 'Please check the provider details and try again.';
    }
  }
  return 'Could not update the provider. Please try again.';
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
  testing,
  onToggleEnabled,
  onRefresh,
  onTest,
  onDelete,
}: {
  provider: LlmProvider;
  busy: boolean;
  testing: boolean;
  onToggleEnabled: () => void;
  onRefresh: () => void;
  onTest: (model: string) => void;
  onDelete: () => void;
}) {
  const models = provider.discovered_models;
  const [testModel, setTestModel] = useState('');
  const selectedModel = models.some((model) => model.id === testModel)
    ? testModel
    : (models[0]?.id ?? '');
  const testSelectId = useId();
  const validatedAt = provider.validation?.validated_at ?? provider.validated_at;
  return (
    <tr className="border-b border-border/60 align-top last:border-0">
      <td className="px-4 py-3">
        <div className="font-medium text-foreground">{provider.name}</div>
        <div className="mt-0.5 break-all text-xs text-foreground-muted">{provider.base_url}</div>
        <div className="mt-0.5 text-xs text-foreground-muted">
          {provider.has_api_key ? 'API key stored' : 'no key'}
        </div>
      </td>
      <td className="px-4 py-3">
        <StatusDot tone={STATUS_TONE[provider.status]} label={STATUS_LABEL[provider.status]} />
        {provider.validation_grandfathered ? (
          <div className="mt-1 max-w-xs break-words text-xs text-foreground-muted">
            Routable from before evidence-based validation. Revalidate with Test connection.
          </div>
        ) : null}
        <ValidationChecks checks={provider.validation_checks} />
        {validatedAt ? (
          <p className="mt-1 text-xs text-foreground-muted">
            Last validated: <time dateTime={validatedAt}>{validatedAt}</time>
          </p>
        ) : null}
        {provider.status === 'error' ? (
          <div className="mt-1 max-w-xs break-words text-xs text-danger">
            {provider.validation
              ? validationDiagnostics(provider.validation)
              : validationMessage(provider.last_error_code)}
          </div>
        ) : null}
        {models.length > 0 ? (
          <div className="mt-2">
            <label htmlFor={testSelectId} className="text-xs font-medium text-foreground-muted">
              Test model for {provider.name}
            </label>
            <select
              id={testSelectId}
              value={selectedModel}
              disabled={busy}
              onChange={(e) => setTestModel(e.target.value)}
              className="mt-1 block w-full max-w-xs rounded-md border border-border bg-surface px-2 py-1 text-xs text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50"
            >
              {models.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.label ?? m.id}
                </option>
              ))}
            </select>
            <p className="mt-1 max-w-xs text-xs text-foreground-muted">
              Test connection sends one minimal request (max 1 output token) to the selected model.
              Your provider may bill for it.
            </p>
            <button
              type="button"
              onClick={() => onTest(selectedModel)}
              disabled={busy || selectedModel === ''}
              className="mt-1 inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs hover:bg-surface-muted disabled:opacity-50"
            >
              {testing ? 'Testing connection…' : 'Test connection'}
            </button>
          </div>
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
            Revalidate
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
          {busy ? (
            <span
              role="status"
              aria-label={testing ? 'Testing connection' : 'Saving provider'}
              className="text-xs text-foreground-muted"
            >
              {testing ? 'Testing connection…' : 'Saving…'}
            </span>
          ) : null}
        </div>
      </td>
    </tr>
  );
}

function ProviderTable({
  providers,
  busyId,
  testingId,
  onToggleEnabled,
  onRefresh,
  onTest,
  onDelete,
}: {
  providers: LlmProvider[];
  busyId: string | null;
  testingId: string | null;
  onToggleEnabled: (p: LlmProvider) => void;
  onRefresh: (p: LlmProvider) => void;
  onTest: (p: LlmProvider, model: string) => void;
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
              testing={testingId === provider.id}
              onToggleEnabled={() => onToggleEnabled(provider)}
              onRefresh={() => onRefresh(provider)}
              onTest={(model) => onTest(provider, model)}
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
  const nameRef = useRef<HTMLInputElement | null>(null);
  const baseUrlRef = useRef<HTMLInputElement | null>(null);
  const apiKeyRef = useRef<SecretInputHandle | null>(null);
  const apiKeyId = useId();
  const canSubmit = name.trim().length > 0 && baseUrl.trim().length > 0 && !submitting;

  const rememberName = useCallback((node: HTMLInputElement | null) => {
    if (node) nameRef.current = node;
  }, []);
  const rememberBaseUrl = useCallback((node: HTMLInputElement | null) => {
    if (node) baseUrlRef.current = node;
  }, []);
  const hardBlankDom = useCallback(() => {
    if (nameRef.current) scrubCredentialInput(nameRef.current);
    if (baseUrlRef.current) scrubCredentialInput(baseUrlRef.current);
    apiKeyRef.current?.reset();
  }, []);
  const clearForm = useCallback(() => {
    hardBlankDom();
    setName('');
    setBaseUrl('');
    setApiKey('');
  }, [hardBlankDom]);
  useCredentialClearer(clearForm);
  useEffect(() => () => hardBlankDom(), [hardBlankDom]);
  // A manager can write again while the request is pending. Reset the actual
  // nodes and reveal state on completion as well as at submit intent.
  useEffect(() => {
    if (!submitting) clearForm();
  }, [submitting, clearForm]);

  const handleSubmit = (event: React.FormEvent) => {
    event.preventDefault();
    if (!canSubmit) return;
    const body: { name: string; base_url: string; api_key?: string } = {
      name: name.trim(),
      base_url: baseUrl.trim(),
    };
    if (apiKey.trim().length > 0) body.api_key = apiKey.trim();
    onSubmit(body);
    clearForm();
  };

  return (
    <form
      onSubmit={handleSubmit}
      autoComplete="off"
      aria-label="Add LLM provider"
      className="grid gap-3 border-t border-border bg-surface-muted/40 px-4 py-4 sm:grid-cols-2"
    >
      <label className="flex flex-col gap-1 text-xs font-medium text-foreground-muted">
        Name
        <input
          ref={rememberName}
          type="text"
          name="llm_provider_display_name"
          autoComplete="off"
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
          ref={rememberBaseUrl}
          type="url"
          name="llm_provider_base_url"
          inputMode="url"
          autoComplete="off"
          autoCapitalize="none"
          spellCheck={false}
          value={baseUrl}
          onChange={(e) => setBaseUrl(e.target.value)}
          placeholder="https://openrouter.ai/api/v1"
          className="rounded-md border border-border bg-surface px-2 py-1.5 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        />
      </label>
      <div className="sm:col-span-2">
        <label htmlFor={apiKeyId} className="mb-1 block text-xs font-medium text-foreground-muted">
          API key (write-only)
        </label>
        <SecretInput
          ref={apiKeyRef}
          id={apiKeyId}
          name="llm_provider_api_key"
          purpose="new-secret"
          revealLabel="API key"
          value={apiKey}
          onValueChange={setApiKey}
          placeholder="sk-…"
          className="w-full rounded-md border border-border bg-surface py-1.5 pl-2 pr-10 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        />
      </div>
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
  const testMutation = useTestLlmProviderConnection();
  const [toast, setToast] = useState<{
    kind: 'ok' | 'error';
    message: string;
    retry?: () => void;
  } | null>(null);
  const providers = query.data?.items ?? [];

  const onOk = (message: string) => setToast({ kind: 'ok', message });
  const onError = (error: unknown) =>
    setToast({ kind: 'error', message: describeWriteError(error) });

  const handleCreate = (body: { name: string; base_url: string; api_key?: string }) => {
    setToast(null);
    createMutation.submit(
      {
        name: body.name,
        provider_type: 'openai_compatible',
        base_url: body.base_url,
        ...(body.api_key === undefined ? {} : { api_key: body.api_key }),
      },
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
      onSuccess: (updated) => {
        if (updated.status === 'error') {
          setToast({
            kind: 'error',
            message: updated.validation
              ? validationDiagnostics(updated.validation)
              : validationMessage(updated.last_error_code),
          });
        } else {
          onOk(`Revalidated ${updated.name} (${updated.status}).`);
        }
      },
      onError,
    });
  };

  const handleTest = (p: LlmProvider, model: string) => {
    setToast(null);
    testMutation.mutate(
      { id: p.id, model },
      {
        onSuccess: (result) => {
          if (result.validation.status === 'ready') {
            onOk(`Test connection passed for ${result.provider.name}. It is now Ready.`);
          } else {
            setToast({
              kind: 'error',
              message: validationDiagnostics(result.validation),
              ...(result.validation.retryable ? { retry: () => handleTest(p, model) } : {}),
            });
          }
        },
        onError,
      },
    );
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
    (testMutation.isPending ? (testMutation.variables?.id ?? null) : null) ??
    (deleteMutation.isPending ? (deleteMutation.variables ?? null) : null);

  return (
    <section aria-labelledby="admin-providers-heading" className="rounded-lg border border-border">
      <header className="border-b border-border px-4 py-3">
        <h2 id="admin-providers-heading" className="text-sm font-semibold text-foreground">
          LLM providers
        </h2>
        <p className="mt-0.5 text-xs text-foreground-muted">
          Register a provider and discover its models. Test connection verifies chat and acceptance
          of the configured credentials before making the provider Ready and routable. The API key
          is stored write-only; only its presence is shown.
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
            {toast.retry ? (
              <button
                type="button"
                onClick={toast.retry}
                disabled={testMutation.isPending}
                className="rounded-md border border-border px-2 py-0.5 text-xs hover:bg-surface disabled:opacity-50"
              >
                Retry Test connection
              </button>
            ) : null}
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
        error={
          query.error
            ? new Error(
                query.error instanceof ApiError && [401, 403].includes(query.error.status)
                  ? describeWriteError(query.error)
                  : 'Could not load the providers. Please try again.',
              )
            : null
        }
        isEmpty={providers.length === 0}
        emptyMessage="No LLM providers registered yet. Add one below."
        onRetry={() => void query.refetch()}
        loadingRows={4}
      >
        <ProviderTable
          providers={providers}
          busyId={busyId}
          testingId={testMutation.isPending ? (testMutation.variables?.id ?? null) : null}
          onToggleEnabled={handleToggleEnabled}
          onRefresh={handleRefresh}
          onTest={handleTest}
          onDelete={handleDelete}
        />
      </PanelBody>
      <AddProviderForm onSubmit={handleCreate} submitting={createMutation.isPending} />
    </section>
  );
}
