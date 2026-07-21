/**
 * ServerDetailDrawer (#228, ADR-0012) — the detail drawer: health + last check,
 * the masked `secret_hint` (never the stored value — AC-2), the discovered tools
 * with a risk-tier badge each, and the "Test connection" action surfacing the
 * refreshed health + discovery result (AC-1). Every async surface has its states.
 *
 * Rendered against a mocked fetch so a contract match is an integration match.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithQuery } from '@/test/renderWithQuery';
import { setAccessToken, clearAccessToken } from '@/api';
import type { McpServer, McpTool, McpToolList } from '@/api';
import { ServerDetailDrawer } from './ServerDetailDrawer';

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

function makeServer(overrides: Partial<McpServer> = {}): McpServer {
  return {
    id: 'm1',
    name: 'Acme Ticketing',
    transport: 'streamable_http',
    endpoint_url: 'https://mcp.acme.com/sse',
    enabled: true,
    status: 'ready',
    last_health_at: '2026-07-03T11:50:00Z',
    discovered_tool_count: 2,
    secret_hint: '••••abcd',
    owner_id: 'u1',
    created_at: '2026-07-01T10:00:00Z',
    updated_at: '2026-07-03T11:50:00Z',
    ...overrides,
  };
}

function makeTool(overrides: Partial<McpTool> = {}): McpTool {
  return {
    name: 'mcp:acme:create_ticket',
    raw_name: 'create_ticket',
    description: 'Open a support ticket.',
    input_schema: { type: 'object' },
    risk_tier: 'T2',
    read_only: false,
    ...overrides,
  };
}

const toolList = (items: McpTool[]): McpToolList => ({ items, next_cursor: null });

/** Route the detail GET, the tools GET, and the test POST to fixtures. */
function mockRoutes(opts: {
  server?: McpServer;
  tools?: McpTool[];
  toolsResponse?: Response;
  onTest?: () => Response;
}) {
  const server = opts.server ?? makeServer();
  return vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
    const url = String(input);
    if (init?.method === 'POST' && url.includes('/mcp-servers/m1/test')) {
      return Promise.resolve(opts.onTest ? opts.onTest() : json(server));
    }
    if (url.includes('/mcp-servers/m1/tools')) {
      return Promise.resolve(opts.toolsResponse ?? json(toolList(opts.tools ?? [makeTool()])));
    }
    if (url.includes('/mcp-servers/m1')) {
      return Promise.resolve(json(server));
    }
    return Promise.resolve(json({ items: [], next_cursor: null }));
  });
}

beforeEach(() => setAccessToken('jwt'));
afterEach(() => {
  clearAccessToken();
  vi.restoreAllMocks();
});

describe('ServerDetailDrawer — content', () => {
  it('renders health, the masked secret hint, and the discovered tools with tier badges', async () => {
    mockRoutes({
      tools: [
        makeTool(),
        makeTool({
          name: 'mcp:acme:read_ticket',
          risk_tier: 'T0',
          read_only: true,
          description: 'Read a ticket (read-only).',
        }),
      ],
    });
    renderWithQuery(<ServerDetailDrawer serverId="m1" onClose={() => {}} />);

    // Health status.
    expect(await screen.findByText(/healthy/i)).toBeInTheDocument();
    // Masked secret hint — NOT a real value.
    expect(screen.getByText('••••abcd')).toBeInTheDocument();

    // Discovered tools with their tier badges.
    const tools = await screen.findByRole('list', { name: /discovered tools/i });
    expect(within(tools).getByText('mcp:acme:create_ticket')).toBeInTheDocument();
    // The T2 tool's risk badge (RiskTierBadge → aria-label "Risk tier T2: …").
    expect(screen.getByLabelText(/risk tier t2/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/risk tier t0/i)).toBeInTheDocument();
  });

  it('renders a LOADING skeleton while the detail resolves', async () => {
    let resolve!: (r: Response) => void;
    vi.spyOn(globalThis, 'fetch').mockReturnValue(
      new Promise<Response>((r) => {
        resolve = r;
      }),
    );
    renderWithQuery(<ServerDetailDrawer serverId="m1" onClose={() => {}} />);
    expect(await screen.findByText(/loading server details/i)).toBeInTheDocument();
    resolve(json(makeServer()));
  });

  it('shows an EMPTY tools state when the server advertised none', async () => {
    mockRoutes({ tools: [] });
    renderWithQuery(<ServerDetailDrawer serverId="m1" onClose={() => {}} />);
    expect(await screen.findByText(/advertised no tools/i)).toBeInTheDocument();
  });

  it('shows an unreachable server clearly (status error + reason), not blank (AC-N)', async () => {
    mockRoutes({
      server: makeServer({
        status: 'error',
        last_error: 'connection refused',
        discovered_tool_count: 0,
      }),
      tools: [],
    });
    renderWithQuery(<ServerDetailDrawer serverId="m1" onClose={() => {}} />);
    expect(await screen.findByText(/unreachable/i)).toBeInTheDocument();
    expect(screen.getByText(/connection refused/i)).toBeInTheDocument();
  });

  it('surfaces a tools-load error with retry', async () => {
    mockRoutes({ toolsResponse: problem(500, 'boom') });
    renderWithQuery(<ServerDetailDrawer serverId="m1" onClose={() => {}} />);
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByRole('button', { name: /retry/i })).toBeInTheDocument();
  });
});

describe('ServerDetailDrawer — test connection (AC-1)', () => {
  it('runs the probe and surfaces the refreshed health + discovery result', async () => {
    const fetchSpy = mockRoutes({
      server: makeServer({ status: 'pending', discovered_tool_count: 0, last_health_at: null }),
      tools: [],
      onTest: () => json(makeServer({ status: 'ready', discovered_tool_count: 5 })),
    });
    const user = userEvent.setup();
    renderWithQuery(<ServerDetailDrawer serverId="m1" onClose={() => {}} />);

    await screen.findByRole('button', { name: /test connection/i });
    await user.click(screen.getByRole('button', { name: /test connection/i }));

    await waitFor(() =>
      expect(
        fetchSpy.mock.calls.some(
          ([u, i]) => String(u).includes('/mcp-servers/m1/test') && i?.method === 'POST',
        ),
      ).toBe(true),
    );
    // The success note reports the discovered-tool count from the probe.
    expect(await screen.findByText(/discovered 5 tools/i)).toBeInTheDocument();
  });

  it('surfaces a failed test trigger as an error (not a crash)', async () => {
    mockRoutes({
      server: makeServer(),
      onTest: () => problem(500, 'probe failed'),
    });
    const user = userEvent.setup();
    renderWithQuery(<ServerDetailDrawer serverId="m1" onClose={() => {}} />);

    await screen.findByRole('button', { name: /test connection/i });
    await user.click(screen.getByRole('button', { name: /test connection/i }));

    expect(await screen.findByRole('alert')).toBeInTheDocument();
  });
});
