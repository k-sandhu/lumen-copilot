/**
 * SourcesPanel (#27, ADR-0009) — the Sources screen across EVERY async state
 * (frontend/AGENTS.md "every state, not just success"): loading skeleton, empty
 * ("Add your first source"), error with retry (401 messaged distinctly), and the
 * success grid with KPIs + connector cards. Plus the per-source flows: re-sync
 * (POST /sources/{id}/sync) and remove behind a confirm (DELETE /sources/{id}).
 * Rendered against a mocked fetch so a contract match is an integration match
 * (ADR-0006 Phase 1).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithQuery } from '@/test/renderWithQuery';
import { setAccessToken, clearAccessToken } from '@/api';
import type { Source, SourceList } from '@/api';
import { SourcesPanel } from './SourcesPanel';

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}
function problem(status: number, title: string): Response {
  return new Response(JSON.stringify({ type: 'about:blank', title, status }), {
    status,
    headers: { 'Content-Type': 'application/problem+json' },
  });
}

function makeSource(overrides: Partial<Source> = {}): Source {
  return {
    id: 's1',
    type: 'web',
    config: { url: 'https://handbook.acme.com/policy', mode: 'page' },
    status: 'ready',
    indexed_count: 12,
    last_synced_at: '2026-06-23T11:50:00Z',
    owner_id: 'u1',
    created_at: '2026-06-23T10:00:00Z',
    updated_at: '2026-06-23T11:50:00Z',
    ...overrides,
  };
}

const list = (items: Source[]): SourceList => ({ items, next_cursor: null });

beforeEach(() => setAccessToken('jwt'));
afterEach(() => {
  clearAccessToken();
  vi.restoreAllMocks();
});

describe('SourcesPanel — states', () => {
  it('renders a LOADING skeleton while the list resolves', async () => {
    let resolve!: (r: Response) => void;
    vi.spyOn(globalThis, 'fetch').mockReturnValue(
      new Promise<Response>((r) => {
        resolve = r;
      }),
    );
    renderWithQuery(<SourcesPanel />);
    expect(await screen.findByText(/loading connected sources/i)).toBeInTheDocument();
    resolve(json(list([makeSource()])));
    expect(await screen.findByRole('article', { name: /handbook\.acme\.com/i })).toBeInTheDocument();
  });

  it('renders the EMPTY state with a primary CTA when there are no sources', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(list([])));
    renderWithQuery(<SourcesPanel />);
    expect(await screen.findByText(/add your first source — paste a link/i)).toBeInTheDocument();
  });

  it('renders an actionable ERROR with retry on a transient failure', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(500, 'Server Error'));
    renderWithQuery(<SourcesPanel />);
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByRole('button', { name: /retry/i })).toBeInTheDocument();
  });

  it('messages a 401 without a pointless retry (INV-4)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(401, 'Unauthorized'));
    renderWithQuery(<SourcesPanel />);
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/session expired/i);
    expect(within(alert).queryByRole('button', { name: /retry/i })).not.toBeInTheDocument();
  });

  it('renders the SUCCESS grid: KPIs + a card with its trust signals', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      json(list([makeSource(), makeSource({ id: 's2', status: 'error', last_error: 'Fetch failed: 503', indexed_count: 0 })])),
    );
    renderWithQuery(<SourcesPanel />);

    const grid = await screen.findByRole('list', { name: /connected sources/i });
    // Two source cards + the "add a connector" tile.
    expect(within(grid).getAllByRole('article')).toHaveLength(2);
    // KPI summary present.
    expect(screen.getByRole('group', { name: /connected sources/i })).toBeInTheDocument();
    // Failed source surfaces its error + a danger status.
    expect(screen.getByText(/fetch failed: 503/i)).toBeInTheDocument();
  });
});

describe('SourcesPanel — actions', () => {
  it('re-syncs a source (POST /sources/{id}/sync)', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
      const url = String(input);
      if (init?.method === 'POST' && url.includes('/sources/s1/sync')) {
        return Promise.resolve(json(makeSource({ status: 'syncing' }), 202));
      }
      return Promise.resolve(json(list([makeSource()])));
    });
    const user = userEvent.setup();
    renderWithQuery(<SourcesPanel />);

    const card = await screen.findByRole('article', { name: /handbook\.acme\.com/i });
    await user.click(within(card).getByRole('button', { name: /sync now/i }));

    await waitFor(() =>
      expect(
        fetchSpy.mock.calls.some(
          ([u, i]) => String(u).includes('/sources/s1/sync') && i?.method === 'POST',
        ),
      ).toBe(true),
    );
  });

  it('surfaces a per-card inline error when a source sync trigger fails', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
      const url = String(input);
      if (init?.method === 'POST' && url.includes('/sources/s1/sync')) {
        return Promise.resolve(problem(503, 'Sync failed'));
      }
      return Promise.resolve(
        json(
          list([
            makeSource(),
            makeSource({
              id: 's2',
              config: { url: 'https://wiki.acme.com/runbook', mode: 'page' },
            }),
          ]),
        ),
      );
    });
    const user = userEvent.setup();
    renderWithQuery(<SourcesPanel />);

    const failedCard = await screen.findByRole('article', { name: /handbook\.acme\.com/i });
    const otherCard = await screen.findByRole('article', { name: /wiki\.acme\.com/i });
    await user.click(within(failedCard).getByRole('button', { name: /sync now/i }));

    expect(await within(failedCard).findByRole('alert')).toHaveTextContent(/sync failed/i);
    expect(within(otherCard).queryByRole('alert')).not.toBeInTheDocument();
  });

  it('clears a failed sync error after a successful retry', async () => {
    let syncAttempts = 0;
    vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
      const url = String(input);
      if (init?.method === 'POST' && url.includes('/sources/s1/sync')) {
        syncAttempts += 1;
        return Promise.resolve(
          syncAttempts === 1
            ? problem(503, 'Sync failed')
            : json(makeSource({ status: 'syncing' }), 202),
        );
      }
      return Promise.resolve(json(list([makeSource()])));
    });
    const user = userEvent.setup();
    renderWithQuery(<SourcesPanel />);

    const card = await screen.findByRole('article', { name: /handbook\.acme\.com/i });
    await user.click(within(card).getByRole('button', { name: /sync now/i }));
    expect(await within(card).findByRole('alert')).toHaveTextContent(/sync failed/i);

    await user.click(within(card).getByRole('button', { name: /sync now/i }));

    await waitFor(() => expect(within(card).queryByRole('alert')).not.toBeInTheDocument());
  });

  it('removes a source only after confirming (DELETE /sources/{id})', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
      const url = String(input);
      if (init?.method === 'DELETE' && url.includes('/sources/s1')) {
        return Promise.resolve(new Response(null, { status: 204 }));
      }
      return Promise.resolve(json(list([makeSource()])));
    });
    const user = userEvent.setup();
    renderWithQuery(<SourcesPanel />);

    const card = await screen.findByRole('article', { name: /handbook\.acme\.com/i });
    await user.click(within(card).getByRole('button', { name: /remove handbook/i }));

    // A confirm dialog gates the destructive delete.
    const confirm = await screen.findByRole('alertdialog');
    expect(confirm).toHaveTextContent(/remove this source\?/i);
    // No DELETE yet — only the initial GET.
    expect(fetchSpy.mock.calls.some(([, i]) => i?.method === 'DELETE')).toBe(false);

    await user.click(within(confirm).getByRole('button', { name: /remove source/i }));
    await waitFor(() =>
      expect(fetchSpy.mock.calls.some(([, i]) => i?.method === 'DELETE')).toBe(true),
    );
  });

  it('cancels the remove without deleting', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(list([makeSource()])));
    const user = userEvent.setup();
    renderWithQuery(<SourcesPanel />);

    const card = await screen.findByRole('article', { name: /handbook\.acme\.com/i });
    await user.click(within(card).getByRole('button', { name: /remove handbook/i }));
    const confirm = await screen.findByRole('alertdialog');
    await user.click(within(confirm).getByRole('button', { name: /cancel/i }));

    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument());
    expect(fetchSpy.mock.calls.some(([, i]) => i?.method === 'DELETE')).toBe(false);
  });

  it('opens the Add-source modal from the header button', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(list([makeSource()])));
    const user = userEvent.setup();
    renderWithQuery(<SourcesPanel />);

    await screen.findByRole('article', { name: /handbook\.acme\.com/i });
    await user.click(screen.getByRole('button', { name: /add source/i }));
    expect(await screen.findByRole('dialog', { name: /add a source/i })).toBeInTheDocument();
  });
});
