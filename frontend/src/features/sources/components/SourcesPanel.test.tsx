/**
 * SourcesPanel (#27, ADR-0009; #455, ADR-0019) — the Sources screen across
 * EVERY async state (frontend/AGENTS.md "every state, not just success"):
 * loading skeleton, empty ("Add your first source"), error with retry (401
 * messaged distinctly), and the success grid with KPIs + connector cards. Plus
 * the per-source flows: re-sync (POST /sources/{id}/sync), remove behind a
 * confirm (DELETE /sources/{id}), and the managed-connector flows — Connect /
 * Reauthorize (POST /sources/{id}/connect → browser navigation to the consent
 * URL), the OAuth return banner for `?connect=ok` and EVERY closed error
 * reason (with the query params cleaned after handling), and the INV-5
 * negatives: a non-admin sees no managed affordances and a direct 403 renders
 * as an inline error state, never a blank pane. Rendered against a mocked
 * fetch so a contract match is an integration match (ADR-0006 Phase 1).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { renderWithQuery } from '@/test/renderWithQuery';
import { setAccessToken, clearAccessToken } from '@/api';
import type { CurrentUser, GdriveSource, SourceList, UserRole, WebSource } from '@/api';
import { SourcesPanel } from './SourcesPanel';
import { navigateToConsent } from '../model/browser';

// The consent redirect leaves the SPA entirely — mock the one navigation seam
// (jsdom cannot navigate).
vi.mock('../model/browser', () => ({ navigateToConsent: vi.fn() }));

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

function makeSource(overrides: Partial<WebSource> = {}): WebSource {
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

function makeGdrive(overrides: Partial<GdriveSource> = {}): GdriveSource {
  return {
    id: 'g1',
    type: 'gdrive',
    config: { mode: 'my_drive' },
    status: 'ready',
    indexed_count: 240,
    last_synced_at: '2026-06-23T11:50:00Z',
    connected_account: { email: 'drive-ops@acme.com' },
    acl_synced_at: '2026-06-23T11:50:00Z',
    unmapped_acl_count: 0,
    reauthorize_required: false,
    owner_id: 'u1',
    created_at: '2026-06-23T10:00:00Z',
    updated_at: '2026-06-23T11:50:00Z',
    ...overrides,
  };
}

const list = (items: SourceList['items']): SourceList => ({ items, next_cursor: null });

function me(roles: UserRole[]): CurrentUser {
  return {
    id: 'u1',
    email: 'user@acme.test',
    tenant_id: 't1',
    tenant_name: 'Acme',
    roles,
    created_at: '2026-01-01T00:00:00Z',
  };
}

/**
 * Route the mocked fetch by URL: /auth/me serves the principal (role-driven
 * admin gating), everything else defers to `handler` (default: the sources
 * list). `extra` intercepts specific calls first (sync/connect/delete).
 */
function installFetch({
  roles = ['member'],
  sources = () => json(list([makeSource()])),
  extra,
}: {
  roles?: UserRole[];
  sources?: () => Response | Promise<Response>;
  extra?: (url: string, init?: RequestInit) => Response | Promise<Response> | null;
} = {}) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const url = String(input);
    if (extra) {
      const handled = extra(url, init as RequestInit | undefined);
      if (handled) return handled;
    }
    if (url.includes('/auth/me')) return json(me(roles));
    return sources();
  });
}

function renderPanel(initialEntry = '/sources') {
  return renderWithQuery(
    <MemoryRouter initialEntries={[initialEntry]}>
      <SourcesPanel />
      <LocationProbe />
    </MemoryRouter>,
  );
}

/** Exposes the live location so tests can assert the query params were cleaned. */
function LocationProbe() {
  const loc = useLocation();
  return <span data-testid="location">{`${loc.pathname}${loc.search}`}</span>;
}

beforeEach(() => setAccessToken('jwt'));
afterEach(() => {
  clearAccessToken();
  vi.restoreAllMocks();
  vi.mocked(navigateToConsent).mockReset();
});

describe('SourcesPanel — states', () => {
  it('renders a LOADING skeleton while the list resolves', async () => {
    let resolve!: (r: Response) => void;
    installFetch({
      sources: () =>
        new Promise<Response>((r) => {
          resolve = r;
        }),
    });
    renderPanel();
    expect(await screen.findByText(/loading connected sources/i)).toBeInTheDocument();
    resolve(json(list([makeSource()])));
    expect(await screen.findByRole('article', { name: /handbook\.acme\.com/i })).toBeInTheDocument();
  });

  it('renders the EMPTY state with a primary CTA when there are no sources', async () => {
    installFetch({ sources: () => json(list([])) });
    renderPanel();
    expect(await screen.findByText(/add your first source — paste a link/i)).toBeInTheDocument();
  });

  it('renders an actionable ERROR with retry on a transient failure', async () => {
    installFetch({ sources: () => problem(500, 'Server Error') });
    renderPanel();
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByRole('button', { name: /retry/i })).toBeInTheDocument();
  });

  it('messages a 401 without a pointless retry (INV-4)', async () => {
    installFetch({ sources: () => problem(401, 'Unauthorized') });
    renderPanel();
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/session expired/i);
    expect(within(alert).queryByRole('button', { name: /retry/i })).not.toBeInTheDocument();
  });

  it('renders the SUCCESS grid: KPIs + a card with its trust signals', async () => {
    installFetch({
      sources: () =>
        json(
          list([
            makeSource(),
            makeSource({ id: 's2', status: 'error', last_error: 'Fetch failed: 503', indexed_count: 0 }),
          ]),
        ),
    });
    renderPanel();

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
    const fetchSpy = installFetch({
      extra: (url, init) =>
        init?.method === 'POST' && url.includes('/sources/s1/sync')
          ? json(makeSource({ status: 'syncing' }), 202)
          : null,
    });
    const user = userEvent.setup();
    renderPanel();

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
    installFetch({
      sources: () =>
        json(
          list([
            makeSource(),
            makeSource({ id: 's2', config: { url: 'https://wiki.acme.com/runbook', mode: 'page' } }),
          ]),
        ),
      extra: (url, init) =>
        init?.method === 'POST' && url.includes('/sources/s1/sync')
          ? problem(503, 'Sync failed')
          : null,
    });
    const user = userEvent.setup();
    renderPanel();

    const failedCard = await screen.findByRole('article', { name: /handbook\.acme\.com/i });
    const otherCard = await screen.findByRole('article', { name: /wiki\.acme\.com/i });
    await user.click(within(failedCard).getByRole('button', { name: /sync now/i }));

    expect(await within(failedCard).findByRole('alert')).toHaveTextContent(/sync failed/i);
    expect(within(otherCard).queryByRole('alert')).not.toBeInTheDocument();
  });

  it('removes a source only after confirming (DELETE /sources/{id})', async () => {
    const fetchSpy = installFetch({
      extra: (url, init) =>
        init?.method === 'DELETE' && url.includes('/sources/s1')
          ? new Response(null, { status: 204 })
          : null,
    });
    const user = userEvent.setup();
    renderPanel();

    const card = await screen.findByRole('article', { name: /handbook\.acme\.com/i });
    await user.click(within(card).getByRole('button', { name: /remove handbook/i }));

    // A confirm dialog gates the destructive delete.
    const confirm = await screen.findByRole('alertdialog');
    expect(confirm).toHaveTextContent(/remove this source\?/i);
    // No DELETE yet.
    expect(fetchSpy.mock.calls.some(([, i]) => i?.method === 'DELETE')).toBe(false);

    await user.click(within(confirm).getByRole('button', { name: /remove source/i }));
    await waitFor(() =>
      expect(fetchSpy.mock.calls.some(([, i]) => i?.method === 'DELETE')).toBe(true),
    );
  });

  it('opens the Add-source modal from the header button', async () => {
    installFetch();
    const user = userEvent.setup();
    renderPanel();

    await screen.findByRole('article', { name: /handbook\.acme\.com/i });
    await user.click(screen.getByRole('button', { name: /add source/i }));
    expect(await screen.findByRole('dialog', { name: /add a source/i })).toBeInTheDocument();
  });
});

describe('SourcesPanel — managed connect flow (ADR-0019 §1)', () => {
  it('Connect on a pending_auth source POSTs /connect and navigates the browser to the consent URL', async () => {
    installFetch({
      roles: ['member', 'admin'],
      sources: () => json(list([makeGdrive({ status: 'pending_auth', connected_account: null })])),
      extra: (url, init) =>
        init?.method === 'POST' && url.includes('/sources/g1/connect')
          ? json({ authorization_url: 'https://accounts.google.com/o/oauth2/v2/auth?state=opaque' })
          : null,
    });
    const user = userEvent.setup();
    renderPanel();

    const card = await screen.findByRole('article', { name: /google drive/i });
    await user.click(within(card).getByRole('button', { name: /connect/i }));

    await waitFor(() =>
      expect(navigateToConsent).toHaveBeenCalledWith(
        'https://accounts.google.com/o/oauth2/v2/auth?state=opaque',
      ),
    );
  });

  it('Reauthorize re-runs connect for a dead-grant source', async () => {
    installFetch({
      roles: ['member', 'admin'],
      sources: () => json(list([makeGdrive({ status: 'error', reauthorize_required: true })])),
      extra: (url, init) =>
        init?.method === 'POST' && url.includes('/sources/g1/connect')
          ? json({ authorization_url: 'https://accounts.google.com/o/oauth2/v2/auth?state=fresh' })
          : null,
    });
    const user = userEvent.setup();
    renderPanel();

    const card = await screen.findByRole('article', { name: /google drive/i });
    await user.click(within(card).getByRole('button', { name: /reauthorize/i }));

    await waitFor(() =>
      expect(navigateToConsent).toHaveBeenCalledWith(
        'https://accounts.google.com/o/oauth2/v2/auth?state=fresh',
      ),
    );
  });

  it('surfaces a direct connect 403 as an inline error state — never a blank pane (INV-5)', async () => {
    installFetch({
      roles: ['member', 'admin'],
      sources: () => json(list([makeGdrive({ status: 'pending_auth', connected_account: null })])),
      extra: (url, init) =>
        init?.method === 'POST' && url.includes('/sources/g1/connect')
          ? problem(403, 'Forbidden')
          : null,
    });
    const user = userEvent.setup();
    renderPanel();

    const card = await screen.findByRole('article', { name: /google drive/i });
    await user.click(within(card).getByRole('button', { name: /connect/i }));

    expect(await within(card).findByRole('alert')).toHaveTextContent(/tenant admin/i);
    expect(navigateToConsent).not.toHaveBeenCalled();
  });

  it('a NON-admin sees no managed-connector affordances on a gdrive card (INV-5)', async () => {
    installFetch({
      roles: ['member'],
      sources: () =>
        json(list([makeGdrive({ status: 'error', reauthorize_required: true }), makeSource()])),
    });
    renderPanel();

    const gdriveCard = await screen.findByRole('article', { name: /google drive/i });
    expect(within(gdriveCard).queryAllByRole('button')).toHaveLength(0);
    // The web card keeps its owner-scoped actions.
    const webCard = screen.getByRole('article', { name: /handbook\.acme\.com/i });
    expect(within(webCard).getByRole('button', { name: /sync now/i })).toBeInTheDocument();
  });
});

describe('SourcesPanel — OAuth return states (the frozen ?connect contract)', () => {
  it('renders the success banner on ?connect=ok and CLEANS the query params', async () => {
    installFetch();
    renderPanel('/sources?connect=ok&source=g1');

    expect(await screen.findByText(/google drive connected/i)).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByTestId('location')).toHaveTextContent(/^\/sources$/),
    );
  });

  it.each([
    ['expired', /expired or was already used/i],
    ['denied', /not authorized/i],
    ['provider_error', /google reported a problem/i],
    ['failed', /something went wrong/i],
  ] as const)('maps connect=error&reason=%s to a clear message', async (reason, expected) => {
    installFetch();
    renderPanel(`/sources?connect=error&reason=${reason}`);

    const banner = await screen.findByRole('alert');
    expect(banner).toHaveTextContent(expected);
    await waitFor(() =>
      expect(screen.getByTestId('location')).toHaveTextContent(/^\/sources$/),
    );
  });

  it('falls back to the generic failure copy on an unknown reason (never blank)', async () => {
    installFetch();
    renderPanel('/sources?connect=error&reason=not_in_the_contract');
    expect(await screen.findByRole('alert')).toHaveTextContent(/something went wrong/i);
  });

  it('the banner is dismissible', async () => {
    installFetch();
    const user = userEvent.setup();
    renderPanel('/sources?connect=ok&source=g1');

    await screen.findByText(/google drive connected/i);
    await user.click(screen.getByRole('button', { name: /dismiss/i }));
    expect(screen.queryByText(/google drive connected/i)).not.toBeInTheDocument();
  });
});
