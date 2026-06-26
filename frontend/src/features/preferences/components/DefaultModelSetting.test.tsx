import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithQuery } from '@/test/renderWithQuery';
import { setAccessToken, clearAccessToken } from '@/api';
import { DefaultModelSetting } from './DefaultModelSetting';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function problemResponse(code: string, status = 422): Response {
  return new Response(
    JSON.stringify({ type: 'about:blank', title: 'Unprocessable Entity', status, code }),
    { status, headers: { 'Content-Type': 'application/problem+json' } },
  );
}

const MODELS = {
  items: [
    {
      id: 'anthropic/claude-opus-4.8',
      label: 'Claude Opus 4.8',
      provider: 'anthropic',
      tier: 'frontier',
      is_default: true,
    },
    {
      id: 'openai/gpt-4o-mini',
      label: 'GPT-4o mini',
      provider: 'openai',
      tier: 'fast',
      is_default: false,
    },
  ],
};

const prefs = (over: Record<string, unknown> = {}) => ({
  default_model_id: 'anthropic/claude-opus-4.8',
  updated_at: '2026-06-25T00:00:00Z',
  ...over,
});

/**
 * Route mocked fetch by URL/method — the component fires GET /preferences and
 * GET /models concurrently on mount (order is not deterministic), so a
 * url-routing mock is more robust than ordered mockResolvedValueOnce.
 */
function mockApi(opts: {
  preferences?: () => Response;
  patch?: (body: unknown) => Response;
  models?: () => Response;
}) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const url = String(input);
    const method = (init?.method ?? 'GET').toUpperCase();
    if (url.includes('/preferences') && method === 'PATCH') {
      const body = init?.body ? JSON.parse(String(init.body)) : undefined;
      return (opts.patch ?? (() => jsonResponse(prefs())))(body);
    }
    if (url.includes('/preferences')) return (opts.preferences ?? (() => jsonResponse(prefs())))();
    if (url.includes('/models')) return (opts.models ?? (() => jsonResponse(MODELS)))();
    throw new Error(`unexpected fetch: ${method} ${url}`);
  });
}

// The preferences query is gated on an access token (only runs once
// authenticated), so set one for the read to fire in tests.
beforeEach(() => setAccessToken('test-jwt'));
afterEach(() => {
  clearAccessToken();
  vi.restoreAllMocks();
});

describe('DefaultModelSetting', () => {
  it('renders the user current default model as the selected option', async () => {
    mockApi({});
    renderWithQuery(<DefaultModelSetting />);

    const select = (await screen.findByRole('combobox', {
      name: /default model/i,
    })) as HTMLSelectElement;
    expect(select.value).toBe('anthropic/claude-opus-4.8');
    // A "use server default" choice is offered (clears the override).
    expect(
      screen.getByRole('option', { name: /server default/i }),
    ).toBeInTheDocument();
  });

  it('shows "server default" selected when no override is set', async () => {
    mockApi({ preferences: () => jsonResponse(prefs({ default_model_id: null })) });
    renderWithQuery(<DefaultModelSetting />);

    const select = (await screen.findByRole('combobox', {
      name: /default model/i,
    })) as HTMLSelectElement;
    // Empty value is the "use server default" sentinel.
    expect(select.value).toBe('');
  });

  it('falls back to the server default and warns when the saved model is gone', async () => {
    // A stored id no longer in the registry would leave the <select> on a value
    // with no matching <option>; mirror ChatView and fall back to server default.
    mockApi({ preferences: () => jsonResponse(prefs({ default_model_id: 'removed/model-x' })) });
    renderWithQuery(<DefaultModelSetting />);

    const select = (await screen.findByRole('combobox', {
      name: /default model/i,
    })) as HTMLSelectElement;
    expect(select.value).toBe('');
    expect(screen.getByText(/no longer available/i)).toBeInTheDocument();
  });

  it('PATCHes the chosen model id when the user picks a different model', async () => {
    let patched: unknown = null;
    mockApi({
      patch: (body) => {
        patched = body;
        return jsonResponse(prefs({ default_model_id: 'openai/gpt-4o-mini' }));
      },
    });
    const user = userEvent.setup();
    renderWithQuery(<DefaultModelSetting />);

    const select = await screen.findByRole('combobox', { name: /default model/i });
    await user.selectOptions(select, 'openai/gpt-4o-mini');

    await waitFor(() => expect(patched).toEqual({ default_model_id: 'openai/gpt-4o-mini' }));
  });

  it('PATCHes null when the user resets to the server default', async () => {
    let patched: unknown = null;
    mockApi({
      preferences: () => jsonResponse(prefs({ default_model_id: 'openai/gpt-4o-mini' })),
      patch: (body) => {
        patched = body;
        return jsonResponse(prefs({ default_model_id: null }));
      },
    });
    const user = userEvent.setup();
    renderWithQuery(<DefaultModelSetting />);

    const select = await screen.findByRole('combobox', { name: /default model/i });
    await user.selectOptions(select, ''); // the "use server default" option

    await waitFor(() => expect(patched).toEqual({ default_model_id: null }));
  });

  it('surfaces an error when the server rejects the model (422 unknown_model)', async () => {
    mockApi({ patch: () => problemResponse('unknown_model') });
    const user = userEvent.setup();
    renderWithQuery(<DefaultModelSetting />);

    const select = await screen.findByRole('combobox', { name: /default model/i });
    await user.selectOptions(select, 'openai/gpt-4o-mini');

    expect(await screen.findByRole('alert')).toBeInTheDocument();
  });

  it('shows an error when the preferences load fails', async () => {
    mockApi({ preferences: () => problemResponse('internal_error', 500) });
    renderWithQuery(<DefaultModelSetting />);

    expect(await screen.findByRole('alert')).toBeInTheDocument();
  });
});
