/**
 * ProvidersPanel state coverage (foundation PR): the per-tenant LLM providers admin
 * surface. Covers the required behaviour plus every async state (frontend/AGENTS.md:
 * not just success):
 *
 * - loading → the provider table (name, base URL, status, discovered-model count +
 *   ids, enable toggle);
 * - the add-form submit calls createLlmProvider with the openai_compatible type;
 * - Refresh calls refreshLlmProvider and surfaces the outcome;
 * - Delete calls deleteLlmProvider;
 * - a discovery-error provider shows its status + last_error (never a blank pane);
 * - a write error (422 bad base URL) surfaces a dismissible alert, not a silent no-op;
 * - the empty state when no providers are registered;
 * - the API key value is never rendered (only the masked secret_hint).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithQuery } from '@/test/renderWithQuery';
import { storageSnapshot } from '@/test/storageSnapshot';
import { ApiError, clearAccessToken, setAccessToken } from '@/api';
import type { LlmProvider, LlmProviderList } from '@/api';
import { useAuthStore } from '@/features/auth';
import { ProvidersPanel } from './ProvidersPanel';

const listLlmProviders = vi.hoisted(() => vi.fn());
const createLlmProvider = vi.hoisted(() => vi.fn());
const updateLlmProvider = vi.hoisted(() => vi.fn());
const deleteLlmProvider = vi.hoisted(() => vi.fn());
const refreshLlmProvider = vi.hoisted(() => vi.fn());
const testLlmProviderConnection = vi.hoisted(() => vi.fn());
vi.mock('@/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api')>();
  return {
    ...actual,
    listLlmProviders,
    createLlmProvider,
    updateLlmProvider,
    deleteLlmProvider,
    refreshLlmProvider,
    testLlmProviderConnection,
  };
});

const PROVIDER_READY: LlmProvider = {
  id: 'p-1',
  name: 'OpenRouter',
  provider_type: 'openai_compatible',
  base_url: 'https://openrouter.ai/api/v1',
  enabled: true,
  status: 'ready',
  last_discovery_at: '2026-07-03T00:00:00Z',
  last_error: null,
  last_error_code: null,
  discovered_models: [
    { id: 'openai/gpt-4o', label: 'GPT-4o' },
    { id: 'meta-llama/llama-3-70b', label: null },
  ],
  has_api_key: true,
  validation_checks: [
    { name: 'config_valid', status: 'passed', code: null },
    { name: 'auth_verified', status: 'passed', code: null },
  ],
  validated_at: '2026-07-03T00:00:00Z',
  validation_grandfathered: false,
  owner_id: 'u-1',
  created_at: '2026-07-03T00:00:00Z',
  updated_at: '2026-07-03T00:00:00Z',
};

const PROVIDER_REACHABLE: LlmProvider = {
  ...PROVIDER_READY,
  id: 'p-3',
  name: 'Unverified',
  status: 'reachable',
  validation_checks: [
    { name: 'config_valid', status: 'passed', code: null },
    { name: 'auth_verified', status: 'unverified', code: null },
  ],
};

const PROVIDER_ERROR: LlmProvider = {
  ...PROVIDER_READY,
  id: 'p-2',
  name: 'Broken',
  status: 'error',
  last_error: 'The provider rejected the API key (401).',
  last_error_code: 'unauthorized',
  discovered_models: [],
  has_api_key: false,
  validation_checks: [{ name: 'catalog_discovered', status: 'failed', code: 'unauthorized' }],
};

const LIST_ONE: LlmProviderList = { items: [PROVIDER_READY] };

beforeEach(() => {
  clearAccessToken();
  useAuthStore.setState({ status: 'unknown' });
  listLlmProviders.mockReset();
  createLlmProvider.mockReset();
  updateLlmProvider.mockReset();
  deleteLlmProvider.mockReset();
  refreshLlmProvider.mockReset();
  testLlmProviderConnection.mockReset();
});

describe('ProvidersPanel', () => {
  it('uses non-login field semantics for provider metadata and a new write-only key', async () => {
    listLlmProviders.mockResolvedValue({ items: [] });
    renderWithQuery(<ProvidersPanel />);
    const form = screen.getByRole('form', { name: /add llm provider/i });

    const name = within(form).getByLabelText(/^name$/i);
    expect(name).toHaveAttribute('name', 'llm_provider_display_name');

    const baseUrl = within(form).getByLabelText(/base url/i);
    expect(baseUrl).toHaveAttribute('type', 'url');
    expect(baseUrl).toHaveAttribute('name', 'llm_provider_base_url');
    expect(baseUrl).toHaveAttribute('inputmode', 'url');
    expect(baseUrl).toHaveAttribute('autocomplete', 'off');
    expect(baseUrl).toHaveAttribute('autocapitalize', 'none');
    expect(baseUrl).toHaveAttribute('spellcheck', 'false');

    const apiKey = within(form).getByLabelText(/api key/i);
    expect(apiKey).toHaveAttribute('type', 'password');
    expect(apiKey).toHaveAttribute('name', 'llm_provider_api_key');
    expect(apiKey).toHaveAttribute('autocomplete', 'new-password');
    expect(apiKey).toHaveAttribute('autocapitalize', 'none');
    expect(apiKey).toHaveAttribute('spellcheck', 'false');
    expect(apiKey).toHaveValue('');
  });

  it('reveals an API key only through an accessible non-submit control', async () => {
    listLlmProviders.mockResolvedValue({ items: [] });
    const fetchSpy = vi.spyOn(globalThis, 'fetch');
    const user = userEvent.setup();
    renderWithQuery(<ProvidersPanel />);

    const apiKey = screen.getByLabelText(/api key/i);
    await user.type(apiKey, 'sk-reveal-only');
    await user.click(screen.getByRole('button', { name: /show api key/i }));

    expect(apiKey).toHaveAttribute('type', 'text');
    expect(screen.getByRole('button', { name: /hide api key/i })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('renders a provider list with status, model count, and model ids', async () => {
    listLlmProviders.mockResolvedValue(LIST_ONE);
    renderWithQuery(<ProvidersPanel />);

    expect(await screen.findByText('OpenRouter')).toBeInTheDocument();
    expect(screen.getByText('https://openrouter.ai/api/v1')).toBeInTheDocument();
    // Status badge + the discovered model ids/labels under the provider.
    expect(screen.getByText('Ready')).toBeInTheDocument();
    // In the model list and again as a test-connection option.
    expect(screen.getAllByText('GPT-4o').length).toBeGreaterThan(0);
    // A model with no label falls back to its id (list + test-connection option).
    expect(screen.getAllByText('meta-llama/llama-3-70b').length).toBeGreaterThan(0);
    // Key presence only — no hint fragment is ever rendered (spec 0029).
    expect(screen.getByText('API key stored')).toBeInTheDocument();
    expect(screen.queryByText(/\*\*\*\*/)).not.toBeInTheDocument();
    // Per-check validation status is visible.
    expect(screen.getByText(/authentication: passed/i)).toBeInTheDocument();
  });

  it('renders a reachable provider as unverified with per-check status', async () => {
    listLlmProviders.mockResolvedValue({ items: [PROVIDER_REACHABLE] });
    renderWithQuery(<ProvidersPanel />);

    expect(await screen.findByText('Unverified')).toBeInTheDocument();
    expect(screen.getByText(/reachable \u2014 unverified/i)).toBeInTheDocument();
    expect(screen.getByText(/authentication: unverified/i)).toBeInTheDocument();
    expect(screen.queryByText('Ready')).not.toBeInTheDocument();
  });

  it('Test connection sends the selected model and announces readiness', async () => {
    listLlmProviders.mockResolvedValue({ items: [PROVIDER_REACHABLE] });
    testLlmProviderConnection.mockResolvedValue({
      provider: { ...PROVIDER_REACHABLE, status: 'ready' },
      validation: {
        status: 'ready',
        checks: [],
        stage: null,
        code: null,
        retryable: false,
        correlation_id: 'req-1',
        tested_model: 'openai/gpt-4o',
        validated_at: '2026-07-03T00:00:00Z',
      },
    });
    const { queryClient } = renderWithQuery(<ProvidersPanel />);
    queryClient.setQueryDefaults(['models'], { gcTime: Infinity });
    queryClient.setQueryData(['models'], []);
    await screen.findByText('Unverified');

    // The billing implication is stated before execution.
    expect(screen.getByText(/may bill for it/i)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /test connection/i }));
    expect(testLlmProviderConnection).toHaveBeenCalledWith('p-3', 'openai/gpt-4o');
    expect(await screen.findByText(/test connection passed/i)).toBeInTheDocument();
    expect(queryClient.getQueryState(['models'])?.isInvalidated).toBe(true);
  });

  it('flags a grandfathered ready provider for revalidation', async () => {
    listLlmProviders.mockResolvedValue({
      items: [{ ...PROVIDER_READY, validation_grandfathered: true }],
    });
    renderWithQuery(<ProvidersPanel />);

    expect(await screen.findByText('OpenRouter')).toBeInTheDocument();
    expect(screen.getByText(/revalidate with test connection/i)).toBeInTheDocument();
  });

  it('ignores raw provider errors and unknown diagnostic codes', async () => {
    const raw = 'HTTPStatusError 10.0.0.9 sk-private-value https://docs.example.test/trace';
    listLlmProviders.mockResolvedValue({
      items: [{ ...PROVIDER_ERROR, last_error: raw, last_error_code: raw }],
    });
    renderWithQuery(<ProvidersPanel />);

    await screen.findByText('Broken');
    expect(document.body.textContent).not.toContain(raw);
    expect(screen.getByText(/provider validation failed/i)).toBeInTheDocument();
  });

  it.each(['transport', 'problem'] as const)(
    'does not render raw %s errors after a write',
    async (kind) => {
      const raw = 'HTTPStatusError 10.0.0.9 sk-private-value https://docs.example.test/trace';
      listLlmProviders.mockResolvedValue(LIST_ONE);
      refreshLlmProvider.mockRejectedValue(
        kind === 'transport'
          ? new Error(raw)
          : new ApiError(raw, 502, { type: 'about:blank', title: raw, detail: raw, status: 502 }),
      );
      renderWithQuery(<ProvidersPanel />);
      await screen.findByText(PROVIDER_READY.name);

      await userEvent.click(screen.getByRole('button', { name: /revalidate/i }));
      const alert = await screen.findByRole('alert');
      expect(alert.textContent).not.toContain(raw);
      expect(alert).toHaveTextContent(/could not update the provider/i);
    },
  );

  it('redacts a read error and lets the admin retry', async () => {
    const raw = 'HTTPStatusError 10.0.0.9 sk-private-value https://docs.example.test/trace';
    listLlmProviders.mockRejectedValueOnce(new Error(raw)).mockResolvedValue(LIST_ONE);
    renderWithQuery(<ProvidersPanel />);

    const alert = await screen.findByRole('alert');
    expect(alert.textContent).not.toContain(raw);
    expect(alert).toHaveTextContent(/could not load the providers/i);
    await userEvent.click(within(alert).getByRole('button', { name: /retry/i }));
    expect(await screen.findByText(PROVIDER_READY.name)).toBeInTheDocument();
  });

  it('shows a pending test, safe failure diagnostics, then an explicit retry', async () => {
    listLlmProviders.mockResolvedValue({ items: [PROVIDER_REACHABLE] });
    let finish!: (value: unknown) => void;
    testLlmProviderConnection.mockReturnValueOnce(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    renderWithQuery(<ProvidersPanel />);
    await screen.findByText('Unverified');
    const select = screen.getByRole('combobox', { name: /test model for unverified/i });
    const selectedModel = PROVIDER_REACHABLE.discovered_models[1]!.id;
    await userEvent.selectOptions(select, selectedModel);
    await userEvent.click(screen.getByRole('button', { name: /^test connection$/i }));
    expect(screen.getByRole('status', { name: /testing connection/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /testing connection/i })).toBeDisabled();
    expect(select).toBeDisabled();
    expect(testLlmProviderConnection).toHaveBeenCalledWith('p-3', selectedModel);

    await act(async () =>
      finish({
        provider: { ...PROVIDER_REACHABLE, status: 'error' },
        validation: {
          status: 'error',
          checks: [],
          stage: 'connection_test',
          code: 'timeout',
          retryable: true,
          correlation_id: 'req-safe-1',
        },
      }),
    );
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/did not respond in time/i);
    expect(alert).toHaveTextContent(/req-safe-1/);
    expect(alert).toHaveTextContent(/connection test/i);
    testLlmProviderConnection.mockResolvedValueOnce({
      provider: { ...PROVIDER_REACHABLE, status: 'ready' },
      validation: { status: 'ready', checks: [], retryable: false, correlation_id: 'req-safe-2' },
    });
    await userEvent.click(within(alert).getByRole('button', { name: /retry test connection/i }));
    expect(await screen.findByText(/test connection passed/i)).toBeInTheDocument();
    expect(testLlmProviderConnection).toHaveBeenCalledTimes(2);
  });

  it('renders persisted safe diagnostics and validation time on reload', async () => {
    listLlmProviders.mockResolvedValue({
      items: [
        {
          ...PROVIDER_ERROR,
          validation: {
            status: 'error',
            checks: [],
            stage: 'discovery',
            code: 'dns_failed',
            retryable: true,
            correlation_id: 'req-history-1',
            validated_at: '2026-10-29T10:00:00Z',
          },
        },
      ],
    });
    renderWithQuery(<ProvidersPanel />);
    await screen.findByText('Broken');

    expect(screen.getByText(/req-history-1/)).toBeInTheDocument();
    expect(screen.getByText(/could not be resolved/i)).toBeInTheDocument();
    expect(screen.getByText(/last validated/i)).toBeInTheDocument();
    expect(document.querySelector('time')).toHaveAttribute('datetime', '2026-10-29T10:00:00Z');
  });

  it('renders a discovery-error provider with its last_error (never a blank pane)', async () => {
    listLlmProviders.mockResolvedValue({ items: [PROVIDER_ERROR] });
    renderWithQuery(<ProvidersPanel />);

    expect(await screen.findByText('Broken')).toBeInTheDocument();
    expect(screen.getByText('Error')).toBeInTheDocument();
    // Only the fixed safe message + stable code render — never raw vendor text.
    expect(screen.getByText(/rejected the API key/i)).toBeInTheDocument();
    expect(screen.getAllByText(/unauthorized/).length).toBeGreaterThan(0);
    expect(screen.queryByText(/HTTPStatusError/)).not.toBeInTheDocument();
  });

  it('the add-form submit calls createLlmProvider (openai_compatible)', async () => {
    listLlmProviders.mockResolvedValue({ items: [] });
    createLlmProvider.mockResolvedValue(PROVIDER_READY);
    const { queryClient } = renderWithQuery(<ProvidersPanel />);

    // Empty state renders, and the add form is always present.
    await screen.findByText(/no llm providers registered/i);
    const form = screen.getByRole('form', { name: /add llm provider/i });
    await userEvent.type(within(form).getByLabelText(/^name$/i), 'OpenRouter');
    await userEvent.type(within(form).getByLabelText(/base url/i), 'https://openrouter.ai/api/v1');
    await userEvent.type(within(form).getByLabelText(/api key/i), 'sk-secret-1234567890');
    await userEvent.click(within(form).getByRole('button', { name: /add provider/i }));

    expect(createLlmProvider).toHaveBeenCalledWith(
      {
        name: 'OpenRouter',
        provider_type: 'openai_compatible',
        base_url: 'https://openrouter.ai/api/v1',
        api_key: 'sk-secret-1234567890',
      },
      expect.any(AbortSignal),
    );
    expect(await screen.findByText(/added openrouter/i)).toBeInTheDocument();
    expect(within(form).getByLabelText(/api key/i)).toHaveValue('');
    expect(
      JSON.stringify(
        queryClient
          .getMutationCache()
          .getAll()
          .map((mutation) => mutation.state),
      ),
    ).not.toContain('sk-secret-1234567890');
    expect(storageSnapshot(window.localStorage)).not.toContain('sk-secret-1234567890');
    expect(storageSnapshot(window.sessionStorage)).not.toContain('sk-secret-1234567890');
    expect(window.location.href).not.toContain('sk-secret-1234567890');
  });

  it('omits an untouched provider API key from the request', async () => {
    listLlmProviders.mockResolvedValue({ items: [] });
    createLlmProvider.mockResolvedValue(PROVIDER_READY);
    const user = userEvent.setup();
    renderWithQuery(<ProvidersPanel />);
    const form = await screen.findByRole('form', { name: /add llm provider/i });

    await user.type(within(form).getByLabelText(/^name$/i), 'No-key provider');
    await user.type(within(form).getByLabelText(/base url/i), 'https://no-key.example/v1');
    await user.click(within(form).getByRole('button', { name: /add provider/i }));

    expect(createLlmProvider).toHaveBeenCalledWith(
      {
        name: 'No-key provider',
        provider_type: 'openai_compatible',
        base_url: 'https://no-key.example/v1',
      },
      expect.any(AbortSignal),
    );
  });

  it('clears provider credential-adjacent fields on identity transition and logout', async () => {
    listLlmProviders.mockResolvedValue({ items: [] });
    setAccessToken('jwt-persona-a');
    useAuthStore.setState({ status: 'authenticated' });
    const user = userEvent.setup();
    renderWithQuery(<ProvidersPanel />);

    await user.type(screen.getByLabelText(/^name$/i), 'Persona A provider');
    await user.type(screen.getByLabelText(/base url/i), 'https://persona-a.example/v1');
    await user.type(screen.getByLabelText(/api key/i), 'persona-a-secret');

    act(() => setAccessToken('jwt-persona-b', 'login'));

    expect(screen.getByLabelText(/^name$/i)).toHaveValue('');
    expect(screen.getByLabelText(/base url/i)).toHaveValue('');
    expect(screen.getByLabelText(/api key/i)).toHaveValue('');

    await user.type(screen.getByLabelText(/^name$/i), 'Persona B provider');
    await user.type(screen.getByLabelText(/base url/i), 'https://persona-b.example/v1');
    await user.type(screen.getByLabelText(/api key/i), 'persona-b-secret');

    act(() => clearAccessToken());

    expect(screen.getByLabelText(/^name$/i)).toHaveValue('');
    expect(screen.getByLabelText(/base url/i)).toHaveValue('');
    expect(screen.getByLabelText(/api key/i)).toHaveValue('');
  });

  it('hard-resets a revealed manager-owned API key after a failed submit (R1-003)', async () => {
    listLlmProviders.mockResolvedValue({ items: [] });
    createLlmProvider.mockRejectedValue(new ApiError('network', 0));
    const user = userEvent.setup();
    renderWithQuery(<ProvidersPanel />);
    const form = await screen.findByRole('form', { name: /add llm provider/i });

    await user.type(within(form).getByLabelText(/^name$/i), 'Persona A provider');
    await user.type(within(form).getByLabelText(/base url/i), 'https://persona-a.example/v1');
    const apiKey = within(form).getByLabelText(/api key/i) as HTMLInputElement;
    await user.click(within(form).getByRole('button', { name: /show api key/i }));
    apiKey.value = 'manager-owned-provider-secret';

    await user.click(within(form).getByRole('button', { name: /add provider/i }));
    await screen.findByRole('alert');

    expect(apiKey.value).toBe('');
    expect(apiKey.type).toBe('password');
    expect(within(form).getByRole('button', { name: /show api key/i })).toHaveAttribute(
      'aria-pressed',
      'false',
    );
  });

  it('hard-blanks manager-owned provider name/base URL on identity loss and unmount (R1-004)', () => {
    listLlmProviders.mockResolvedValue({ items: [] });
    setAccessToken('jwt-persona-a');
    useAuthStore.setState({ status: 'authenticated' });
    const view = renderWithQuery(<ProvidersPanel />);
    const name = screen.getByLabelText(/^name$/i) as HTMLInputElement;
    const baseUrl = screen.getByLabelText(/base url/i) as HTMLInputElement;

    name.value = 'manager-owned-persona-a-name';
    baseUrl.value = 'https://manager-owned-persona-a.example/v1';
    act(() => clearAccessToken());

    expect(name.value).toBe('');
    expect(baseUrl.value).toBe('');

    name.value = 'retained-manager-name';
    baseUrl.value = 'https://retained-manager.example/v1';
    view.unmount();

    expect(name.value).toBe('');
    expect(baseUrl.value).toBe('');
  });

  it('clears the detached API-key control on unmount', async () => {
    listLlmProviders.mockResolvedValue({ items: [] });
    const user = userEvent.setup();
    const view = renderWithQuery(<ProvidersPanel />);
    const apiKey = screen.getByLabelText(/api key/i) as HTMLInputElement;
    await user.type(apiKey, 'unmount-provider-secret');

    view.unmount();

    expect(apiKey.value).toBe('');
  });

  it.each(['success', 'failure'] as const)(
    'blanks manager writes made during submission when it settles with %s',
    async (outcome) => {
      listLlmProviders.mockResolvedValue({ items: [] });
      let resolve!: (provider: LlmProvider) => void;
      let reject!: (error: unknown) => void;
      createLlmProvider.mockReturnValue(
        new Promise<LlmProvider>((done, fail) => {
          resolve = done;
          reject = fail;
        }),
      );
      const user = userEvent.setup();
      renderWithQuery(<ProvidersPanel />);
      const form = screen.getByRole('form', { name: /add llm provider/i });
      const name = within(form).getByLabelText(/^name$/i) as HTMLInputElement;
      const baseUrl = within(form).getByLabelText(/base url/i) as HTMLInputElement;
      const apiKey = within(form).getByLabelText(/api key/i) as HTMLInputElement;
      await user.type(name, 'OpenRouter');
      await user.type(baseUrl, 'https://openrouter.ai/api/v1');
      await user.click(within(form).getByRole('button', { name: /add provider/i }));
      await screen.findByRole('button', { name: /adding/i });

      name.value = 'manager-name-during-submit';
      baseUrl.value = 'https://manager-during-submit.example';
      apiKey.value = 'manager-secret-during-submit';
      apiKey.type = 'text';
      await act(async () => {
        if (outcome === 'success') resolve(PROVIDER_READY);
        else reject(new ApiError('network', 0));
      });

      expect(name.value).toBe('');
      expect(baseUrl.value).toBe('');
      expect(apiKey.value).toBe('');
      expect(apiKey.type).toBe('password');
    },
  );

  it('Revalidate calls refreshLlmProvider and surfaces the outcome', async () => {
    listLlmProviders.mockResolvedValue(LIST_ONE);
    refreshLlmProvider.mockResolvedValue({ ...PROVIDER_READY, status: 'ready' });
    renderWithQuery(<ProvidersPanel />);
    await screen.findByText('OpenRouter');

    await userEvent.click(screen.getByRole('button', { name: /revalidate/i }));
    expect(refreshLlmProvider).toHaveBeenCalledWith('p-1');
    expect(await screen.findByText(/revalidated openrouter/i)).toBeInTheDocument();
  });

  it('Delete calls deleteLlmProvider', async () => {
    listLlmProviders.mockResolvedValue(LIST_ONE);
    deleteLlmProvider.mockResolvedValue(undefined);
    renderWithQuery(<ProvidersPanel />);
    await screen.findByText('OpenRouter');

    await userEvent.click(screen.getByRole('button', { name: /delete openrouter/i }));
    expect(deleteLlmProvider).toHaveBeenCalledWith('p-1');
    expect(await screen.findByText(/removed openrouter/i)).toBeInTheDocument();
  });

  it('toggling enabled PATCHes enabled=false', async () => {
    listLlmProviders.mockResolvedValue(LIST_ONE);
    updateLlmProvider.mockResolvedValue({ ...PROVIDER_READY, enabled: false });
    renderWithQuery(<ProvidersPanel />);
    await screen.findByText('OpenRouter');

    await userEvent.click(screen.getByRole('switch', { name: /disable openrouter/i }));
    expect(updateLlmProvider).toHaveBeenCalledWith('p-1', { enabled: false });
  });

  it('surfaces a 422 base-url write error as a dismissible alert', async () => {
    listLlmProviders.mockResolvedValue({ items: [] });
    createLlmProvider.mockRejectedValue(
      new ApiError('unprocessable', 422, {
        type: 'about:blank',
        title: 'Validation error',
        status: 422,
        code: 'base_url_scheme',
      }),
    );
    renderWithQuery(<ProvidersPanel />);
    await screen.findByText(/no llm providers registered/i);

    const form = screen.getByRole('form', { name: /add llm provider/i });
    await userEvent.type(within(form).getByLabelText(/^name$/i), 'Bad');
    await userEvent.type(within(form).getByLabelText(/base url/i), 'http://insecure.example');
    await userEvent.click(within(form).getByRole('button', { name: /add provider/i }));

    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText(/https/i)).toBeInTheDocument();
  });

  it('shows an empty state when no providers are registered', async () => {
    listLlmProviders.mockResolvedValue({ items: [] });
    renderWithQuery(<ProvidersPanel />);
    expect(await screen.findByText(/no llm providers registered/i)).toBeInTheDocument();
  });

  it('surfaces a read 403 as an actionable panel error (INV-5)', async () => {
    listLlmProviders.mockRejectedValue(new ApiError('forbidden', 403));
    renderWithQuery(<ProvidersPanel />);
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText(/admin role/i)).toBeInTheDocument();
  });
});
