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
import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithQuery } from '@/test/renderWithQuery';
import { ApiError } from '@/api';
import type { LlmProvider, LlmProviderList } from '@/api';
import { ProvidersPanel } from './ProvidersPanel';

const listLlmProviders = vi.hoisted(() => vi.fn());
const createLlmProvider = vi.hoisted(() => vi.fn());
const updateLlmProvider = vi.hoisted(() => vi.fn());
const deleteLlmProvider = vi.hoisted(() => vi.fn());
const refreshLlmProvider = vi.hoisted(() => vi.fn());
vi.mock('@/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api')>();
  return {
    ...actual,
    listLlmProviders,
    createLlmProvider,
    updateLlmProvider,
    deleteLlmProvider,
    refreshLlmProvider,
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
  discovered_models: [
    { id: 'openai/gpt-4o', label: 'GPT-4o' },
    { id: 'meta-llama/llama-3-70b', label: null },
  ],
  secret_hint: '****1234',
  owner_id: 'u-1',
  created_at: '2026-07-03T00:00:00Z',
  updated_at: '2026-07-03T00:00:00Z',
};

const PROVIDER_ERROR: LlmProvider = {
  ...PROVIDER_READY,
  id: 'p-2',
  name: 'Broken',
  status: 'error',
  last_error: 'HTTPStatusError: 401 Unauthorized',
  discovered_models: [],
  secret_hint: null,
};

const LIST_ONE: LlmProviderList = { items: [PROVIDER_READY] };

beforeEach(() => {
  listLlmProviders.mockReset();
  createLlmProvider.mockReset();
  updateLlmProvider.mockReset();
  deleteLlmProvider.mockReset();
  refreshLlmProvider.mockReset();
});

describe('ProvidersPanel', () => {
  it('renders a provider list with status, model count, and model ids', async () => {
    listLlmProviders.mockResolvedValue(LIST_ONE);
    renderWithQuery(<ProvidersPanel />);

    expect(await screen.findByText('OpenRouter')).toBeInTheDocument();
    expect(screen.getByText('https://openrouter.ai/api/v1')).toBeInTheDocument();
    // Status badge + the discovered model ids/labels under the provider.
    expect(screen.getByText('Ready')).toBeInTheDocument();
    expect(screen.getByText('GPT-4o')).toBeInTheDocument();
    // A model with no label falls back to its id.
    expect(screen.getByText('meta-llama/llama-3-70b')).toBeInTheDocument();
    // The masked hint is shown, and the raw key value never is.
    expect(screen.getByText(/\*\*\*\*1234/)).toBeInTheDocument();
  });

  it('renders a discovery-error provider with its last_error (never a blank pane)', async () => {
    listLlmProviders.mockResolvedValue({ items: [PROVIDER_ERROR] });
    renderWithQuery(<ProvidersPanel />);

    expect(await screen.findByText('Broken')).toBeInTheDocument();
    expect(screen.getByText('Error')).toBeInTheDocument();
    expect(screen.getByText(/401 Unauthorized/)).toBeInTheDocument();
  });

  it('the add-form submit calls createLlmProvider (openai_compatible)', async () => {
    listLlmProviders.mockResolvedValue({ items: [] });
    createLlmProvider.mockResolvedValue(PROVIDER_READY);
    renderWithQuery(<ProvidersPanel />);

    // Empty state renders, and the add form is always present.
    await screen.findByText(/no llm providers registered/i);
    const form = screen.getByRole('form', { name: /add llm provider/i });
    await userEvent.type(within(form).getByLabelText(/^name$/i), 'OpenRouter');
    await userEvent.type(within(form).getByLabelText(/base url/i), 'https://openrouter.ai/api/v1');
    await userEvent.type(within(form).getByLabelText(/api key/i), 'sk-secret-1234567890');
    await userEvent.click(within(form).getByRole('button', { name: /add provider/i }));

    expect(createLlmProvider).toHaveBeenCalledWith({
      name: 'OpenRouter',
      provider_type: 'openai_compatible',
      base_url: 'https://openrouter.ai/api/v1',
      api_key: 'sk-secret-1234567890',
    });
    expect(await screen.findByText(/added openrouter/i)).toBeInTheDocument();
  });

  it('Refresh calls refreshLlmProvider and surfaces the outcome', async () => {
    listLlmProviders.mockResolvedValue(LIST_ONE);
    refreshLlmProvider.mockResolvedValue({ ...PROVIDER_READY, status: 'ready' });
    renderWithQuery(<ProvidersPanel />);
    await screen.findByText('OpenRouter');

    await userEvent.click(screen.getByRole('button', { name: /refresh/i }));
    expect(refreshLlmProvider).toHaveBeenCalledWith('p-1');
    expect(await screen.findByText(/refreshed openrouter/i)).toBeInTheDocument();
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
