/**
 * ServersPanel (#228, ADR-0012) — the MCP-servers screen across EVERY async state
 * (frontend/AGENTS.md "every state, not just success"): loading skeleton, empty
 * ("Register your first MCP server"), error with retry (401 messaged distinctly),
 * and the success grid with KPIs + server cards showing health + tool counts.
 * Plus the per-server flows: test-connection (POST /mcp-servers/{id}/test),
 * enable/disable (PATCH), and remove behind a confirm (DELETE).
 *
 * Rendered against a mocked fetch so a contract match is an integration match
 * (ADR-0006 Phase 1).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithQuery } from '@/test/renderWithQuery';
import { setAccessToken, clearAccessToken } from '@/api';
import type { McpServer, McpServerList } from '@/api';
import { ServersPanel } from './ServersPanel';

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}
function problem(status: number, title: string, code?: string): Response {
  return new Response(JSON.stringify({ type: 'about:blank', title, status, code }), {
    status,
    headers: { 'Content-Type': 'application/problem+json' },
  });
}

function makeServer(overrides: Partial<McpServer> = {}): McpServer {
  return {
    id: 'm1',
    name: 'Acme Ticketing',
    transport: 'streamable_http',
    endpoint_url: 'https://mcp.acme.com/sse',
    enabled: true,
    status: 'ready',
    last_health_at: '2026-07-03T11:50:00Z',
    discovered_tool_count: 4,
    secret_hint: '••••abcd',
    owner_id: 'u1',
    created_at: '2026-07-01T10:00:00Z',
    updated_at: '2026-07-03T11:50:00Z',
    ...overrides,
  };
}

const list = (items: McpServer[]): McpServerList => ({ items, next_cursor: null });

beforeEach(() => setAccessToken('jwt'));
afterEach(() => {
  clearAccessToken();
  vi.restoreAllMocks();
});

describe('ServersPanel — states', () => {
  it('renders a LOADING skeleton while the list resolves', async () => {
    let resolve!: (r: Response) => void;
    vi.spyOn(globalThis, 'fetch').mockReturnValue(
      new Promise<Response>((r) => {
        resolve = r;
      }),
    );
    renderWithQuery(<ServersPanel />);
    expect(await screen.findByText(/loading mcp servers/i)).toBeInTheDocument();
    resolve(json(list([makeServer()])));
    expect(
      await screen.findByRole('article', { name: /acme ticketing/i }),
    ).toBeInTheDocument();
  });

  it('renders the EMPTY state with a primary CTA when there are no servers', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(list([])));
    renderWithQuery(<ServersPanel />);
    expect(
      await screen.findByText(/register your first mcp server/i),
    ).toBeInTheDocument();
  });

  it('renders an actionable ERROR with retry on a transient failure', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(500, 'Server Error'));
    renderWithQuery(<ServersPanel />);
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByRole('button', { name: /retry/i })).toBeInTheDocument();
  });

  it('messages a 401 without a pointless retry (INV-4)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(401, 'Unauthorized'));
    renderWithQuery(<ServersPanel />);
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/session expired/i);
    expect(within(alert).queryByRole('button', { name: /retry/i })).not.toBeInTheDocument();
  });

  it('renders the SUCCESS grid: KPIs + a card with health + tool count', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      json(
        list([
          makeServer(),
          makeServer({
            id: 'm2',
            name: 'Broken Server',
            status: 'error',
            last_error: 'handshake timed out',
            discovered_tool_count: 0,
          }),
        ]),
      ),
    );
    renderWithQuery(<ServersPanel />);

    const grid = await screen.findByRole('list', { name: /registered mcp servers/i });
    expect(within(grid).getAllByRole('article')).toHaveLength(2);
    // KPI summary present.
    expect(screen.getByRole('group', { name: /registered servers/i })).toBeInTheDocument();
    // A downed server surfaces its error + a danger status (not blank) — AC-N.
    const broken = within(grid).getByRole('article', { name: /broken server/i });
    expect(within(broken).getByText(/handshake timed out/i)).toBeInTheDocument();
    // The healthy card shows its discovered-tool count.
    const healthy = within(grid).getByRole('article', { name: /acme ticketing/i });
    expect(within(healthy).getByText('4')).toBeInTheDocument();
  });
});

describe('ServersPanel — actions', () => {
  it('tests a connection (POST /mcp-servers/{id}/test)', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
      const url = String(input);
      if (init?.method === 'POST' && url.includes('/mcp-servers/m1/test')) {
        return Promise.resolve(json(makeServer({ status: 'ready', discovered_tool_count: 6 })));
      }
      return Promise.resolve(json(list([makeServer()])));
    });
    const user = userEvent.setup();
    renderWithQuery(<ServersPanel />);

    const card = await screen.findByRole('article', { name: /acme ticketing/i });
    await user.click(within(card).getByRole('button', { name: /test connection/i }));

    await waitFor(() =>
      expect(
        fetchSpy.mock.calls.some(
          ([u, i]) => String(u).includes('/mcp-servers/m1/test') && i?.method === 'POST',
        ),
      ).toBe(true),
    );
  });

  it('surfaces a per-card inline error when a test trigger fails', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
      const url = String(input);
      if (init?.method === 'POST' && url.includes('/mcp-servers/m1/test')) {
        return Promise.resolve(problem(500, 'probe failed'));
      }
      return Promise.resolve(json(list([makeServer(), makeServer({ id: 'm2', name: 'Other' })])));
    });
    const user = userEvent.setup();
    renderWithQuery(<ServersPanel />);

    const failing = await screen.findByRole('article', { name: /acme ticketing/i });
    const other = await screen.findByRole('article', { name: /^MCP server: Other$/i });
    await user.click(within(failing).getByRole('button', { name: /test connection/i }));

    expect(await within(failing).findByRole('alert')).toBeInTheDocument();
    expect(within(other).queryByRole('alert')).not.toBeInTheDocument();
  });

  it('toggles enabled (PATCH /mcp-servers/{id} with {enabled})', async () => {
    let patchBody: unknown = null;
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
      const url = String(input);
      if (init?.method === 'PATCH' && url.includes('/mcp-servers/m1')) {
        patchBody = init.body ? JSON.parse(String(init.body)) : null;
        return Promise.resolve(json(makeServer({ enabled: false })));
      }
      return Promise.resolve(json(list([makeServer({ enabled: true })])));
    });
    const user = userEvent.setup();
    renderWithQuery(<ServersPanel />);

    const card = await screen.findByRole('article', { name: /acme ticketing/i });
    await user.click(within(card).getByRole('switch', { name: /disable acme/i }));

    await waitFor(() =>
      expect(fetchSpy.mock.calls.some(([, i]) => i?.method === 'PATCH')).toBe(true),
    );
    expect(patchBody).toEqual({ enabled: false });
  });

  it('removes a server only after confirming (DELETE /mcp-servers/{id})', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
      const url = String(input);
      if (init?.method === 'DELETE' && url.includes('/mcp-servers/m1')) {
        return Promise.resolve(new Response(null, { status: 204 }));
      }
      return Promise.resolve(json(list([makeServer()])));
    });
    const user = userEvent.setup();
    renderWithQuery(<ServersPanel />);

    const card = await screen.findByRole('article', { name: /acme ticketing/i });
    await user.click(within(card).getByRole('button', { name: /remove acme/i }));

    const confirm = await screen.findByRole('alertdialog');
    expect(confirm).toHaveTextContent(/remove this mcp server\?/i);
    // No DELETE yet — the confirm gates it.
    expect(fetchSpy.mock.calls.some(([, i]) => i?.method === 'DELETE')).toBe(false);

    await user.click(within(confirm).getByRole('button', { name: /remove server/i }));
    await waitFor(() =>
      expect(fetchSpy.mock.calls.some(([, i]) => i?.method === 'DELETE')).toBe(true),
    );
  });

  it('cancels the remove without deleting', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(list([makeServer()])));
    const user = userEvent.setup();
    renderWithQuery(<ServersPanel />);

    const card = await screen.findByRole('article', { name: /acme ticketing/i });
    await user.click(within(card).getByRole('button', { name: /remove acme/i }));
    const confirm = await screen.findByRole('alertdialog');
    await user.click(within(confirm).getByRole('button', { name: /cancel/i }));

    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument());
    expect(fetchSpy.mock.calls.some(([, i]) => i?.method === 'DELETE')).toBe(false);
  });

  it('opens the register modal from the header button', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(list([makeServer()])));
    const user = userEvent.setup();
    renderWithQuery(<ServersPanel />);

    await screen.findByRole('article', { name: /acme ticketing/i });
    await user.click(screen.getByRole('button', { name: /register server/i }));
    expect(
      await screen.findByRole('dialog', { name: /register an mcp server/i }),
    ).toBeInTheDocument();
  });
});
