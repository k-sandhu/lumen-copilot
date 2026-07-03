/**
 * RegisterServerModal (#228, ADR-0012 §5) — the register form: client-side
 * validation, the 422 endpoint-blocked / unsupported-transport reason surfaced
 * inline (AC-N), and — critically — the WRITE-ONLY secret field: it never shows a
 * stored value and only ever SENDS what the user typed (AC-2 / CC-C #209).
 *
 * Rendered against a mocked fetch so a contract match is an integration match.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithQuery } from '@/test/renderWithQuery';
import { setAccessToken, clearAccessToken } from '@/api';
import type { McpServer } from '@/api';
import { RegisterServerModal } from './RegisterServerModal';

function json(body: unknown, status = 201): Response {
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
    status: 'pending',
    discovered_tool_count: 0,
    secret_hint: '••••abcd',
    owner_id: 'u1',
    created_at: '2026-07-01T10:00:00Z',
    updated_at: '2026-07-01T10:00:00Z',
    ...overrides,
  };
}

beforeEach(() => setAccessToken('jwt'));
afterEach(() => {
  clearAccessToken();
  vi.restoreAllMocks();
});

describe('RegisterServerModal — validation', () => {
  it('rejects a non-https endpoint client-side (no POST)', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch');
    const user = userEvent.setup();
    renderWithQuery(<RegisterServerModal open onClose={() => {}} />);

    await user.type(screen.getByLabelText(/^name$/i), 'Acme');
    await user.type(screen.getByLabelText(/endpoint url/i), 'http://mcp.acme.com');
    await user.click(screen.getByRole('button', { name: /register server/i }));

    expect(await screen.findByRole('alert')).toHaveTextContent(/https/i);
    expect(fetchSpy.mock.calls.some(([, i]) => i?.method === 'POST')).toBe(false);
  });

  it('surfaces the 422 endpoint-blocked reason inline and stays open (AC-N)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      problem(422, 'Endpoint blocked', 'endpoint_blocked'),
    );
    const user = userEvent.setup();
    renderWithQuery(<RegisterServerModal open onClose={() => {}} />);

    await user.type(screen.getByLabelText(/^name$/i), 'Acme');
    await user.type(screen.getByLabelText(/endpoint url/i), 'https://169.254.169.254/latest');
    await user.click(screen.getByRole('button', { name: /register server/i }));

    expect(await screen.findByRole('alert')).toHaveTextContent(/blocked|private|reached safely/i);
    // Still open — the user can fix the endpoint.
    expect(screen.getByRole('dialog', { name: /register an mcp server/i })).toBeInTheDocument();
  });

  it('surfaces the 422 unsupported-transport reason inline', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      problem(422, 'Unsupported transport', 'unsupported_transport'),
    );
    const user = userEvent.setup();
    renderWithQuery(<RegisterServerModal open onClose={() => {}} />);

    await user.type(screen.getByLabelText(/^name$/i), 'Acme');
    await user.type(screen.getByLabelText(/endpoint url/i), 'https://mcp.acme.com/sse');
    await user.click(screen.getByRole('button', { name: /register server/i }));

    expect(await screen.findByRole('alert')).toHaveTextContent(/transport|streamable|sse/i);
  });
});

describe('RegisterServerModal — write-only secret (AC-2 / CC-C)', () => {
  it('never renders a stored value — the secret field starts empty and is masked', () => {
    renderWithQuery(<RegisterServerModal open onClose={() => {}} />);
    const secret = screen.getByLabelText(/^secret/i) as HTMLInputElement;
    // Write-only: masked input, never pre-filled from any stored value.
    expect(secret).toHaveAttribute('type', 'password');
    expect(secret.value).toBe('');
    // The hint tells the user it is never displayed back.
    expect(screen.getByText(/never display it back|write-only/i)).toBeInTheDocument();
  });

  it('SENDS the typed secret as write-only auth, then it is gone once the modal closes', async () => {
    const posted: { auth?: { type: string; value: string } }[] = [];
    vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
      const url = String(input);
      if (init?.method === 'POST' && url.includes('/mcp-servers') && init.body) {
        posted.push(JSON.parse(String(init.body)) as { auth?: { type: string; value: string } });
        return Promise.resolve(json(makeServer()));
      }
      return Promise.resolve(json({ items: [], next_cursor: null }, 200));
    });
    const onClose = vi.fn();
    const user = userEvent.setup();
    renderWithQuery(<RegisterServerModal open onClose={onClose} />);

    await user.type(screen.getByLabelText(/^name$/i), 'Acme');
    await user.type(screen.getByLabelText(/endpoint url/i), 'https://mcp.acme.com/sse');
    await user.type(screen.getByLabelText(/^secret/i), 'super-secret-token');
    await user.click(screen.getByRole('button', { name: /register server/i }));

    // The modal signals close on success — the parent unmounts it in production.
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    // The secret rode along WRITE-ONLY in the request body (never a GET/echo).
    expect(posted.at(-1)?.auth).toEqual({ type: 'bearer', value: 'super-secret-token' });
    // It never leaves the write-only field: the app never reads a secret value
    // back from the server (no request ever returns `auth`/`value`), so nothing
    // outside this masked input can display it.
    const secret = screen.getByLabelText(/^secret/i) as HTMLInputElement;
    expect(secret.type).toBe('password');
  });

  it('registers with NO auth when the secret is left blank', async () => {
    const posted: { auth?: unknown }[] = [];
    vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
      const url = String(input);
      if (init?.method === 'POST' && url.includes('/mcp-servers') && init.body) {
        posted.push(JSON.parse(String(init.body)) as { auth?: unknown });
        return Promise.resolve(json(makeServer({ secret_hint: null })));
      }
      return Promise.resolve(json({ items: [], next_cursor: null }, 200));
    });
    const user = userEvent.setup();
    renderWithQuery(<RegisterServerModal open onClose={() => {}} />);

    await user.type(screen.getByLabelText(/^name$/i), 'Acme');
    await user.type(screen.getByLabelText(/endpoint url/i), 'https://mcp.acme.com/sse');
    await user.click(screen.getByRole('button', { name: /register server/i }));

    await waitFor(() => expect(posted.length).toBeGreaterThan(0));
    expect(posted.at(-1)?.auth).toBeUndefined();
  });
});
