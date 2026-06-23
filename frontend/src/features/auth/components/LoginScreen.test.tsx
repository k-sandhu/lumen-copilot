/**
 * LoginScreen behavior across states (frontend/AGENTS.md: every state, not just
 * success) and the auth ACs:
 *   AC-1: submitting credentials calls POST /auth/login and stores the token.
 *   AC-4: bad credentials show a SINGLE GENERIC error (no account-existence
 *         disclosure — the same message regardless of which field was wrong).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithQuery } from '@/test/renderWithQuery';
import { LoginScreen } from './LoginScreen';
import { useAuthStore } from '../model/authStore';
import { clearAccessToken, getAccessToken } from '@/api';

function tokenResponse(): Response {
  return new Response(
    JSON.stringify({ access_token: 'jwt-login', token_type: 'bearer', expires_in: 900 }),
    { status: 200, headers: { 'Content-Type': 'application/json' } },
  );
}

function unauthorized(detail = 'Invalid email or password.'): Response {
  return new Response(
    JSON.stringify({ type: 'about:blank', title: 'Unauthorized', status: 401, detail }),
    { status: 401, headers: { 'Content-Type': 'application/problem+json' } },
  );
}

beforeEach(() => {
  clearAccessToken();
  useAuthStore.setState({ status: 'unauthenticated' });
});
afterEach(() => vi.restoreAllMocks());

describe('LoginScreen', () => {
  it('renders an accessible email/password form (AC-1)', () => {
    renderWithQuery(<LoginScreen />);
    expect(screen.getByLabelText(/email/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/password/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /sign in/i })).toBeInTheDocument();
  });

  it('renders the branded front door (brand cell + heading, #116)', () => {
    renderWithQuery(<LoginScreen />);
    // Brand cell names the product (never "Beacon") and the heading frames the
    // unauthenticated front door.
    expect(screen.getByText(/lumen copilot/i)).toBeInTheDocument();
    expect(
      screen.getByRole('heading', { name: /sign in to your workspace/i }),
    ).toBeInTheDocument();
  });

  it('submits credentials, stores the token, and marks authenticated (AC-1)', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(tokenResponse());
    const user = userEvent.setup();
    renderWithQuery(<LoginScreen />);

    await user.type(screen.getByLabelText(/email/i), 'kw@acme.test');
    await user.type(screen.getByLabelText(/password/i), 'correct horse');
    await user.click(screen.getByRole('button', { name: /sign in/i }));

    await waitFor(() => expect(getAccessToken()).toBe('jwt-login'));
    expect(useAuthStore.getState().status).toBe('authenticated');

    const init = fetchSpy.mock.calls[0]?.[1] as RequestInit;
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body as string)).toMatchObject({ email: 'kw@acme.test' });
  });

  it('shows a single GENERIC error on bad credentials (AC-4, no leak)', async () => {
    // Even though the server detail says "Invalid email or password", the UI
    // must show its OWN generic copy — and identically for any wrong field, so
    // nothing reveals whether the account exists.
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(unauthorized());
    const user = userEvent.setup();
    renderWithQuery(<LoginScreen />);

    await user.type(screen.getByLabelText(/email/i), 'nope@acme.test');
    await user.type(screen.getByLabelText(/password/i), 'wrong');
    await user.click(screen.getByRole('button', { name: /sign in/i }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/incorrect email or password/i);
    // It must NOT disclose which field, nor echo a server-specific account hint.
    expect(alert).not.toHaveTextContent(/email address/i);
    expect(alert).not.toHaveTextContent(/no such (account|user)/i);
    expect(getAccessToken()).toBeNull();
    expect(useAuthStore.getState().status).toBe('unauthenticated');
  });

  it('disables submit and shows a busy state while the request is in flight', async () => {
    let resolve!: (r: Response) => void;
    vi.spyOn(globalThis, 'fetch').mockReturnValue(
      new Promise<Response>((r) => {
        resolve = r;
      }),
    );
    const user = userEvent.setup();
    renderWithQuery(<LoginScreen />);

    await user.type(screen.getByLabelText(/email/i), 'kw@acme.test');
    await user.type(screen.getByLabelText(/password/i), 'pw');
    await user.click(screen.getByRole('button', { name: /sign in/i }));

    const button = screen.getByRole('button', { name: /signing in/i });
    expect(button).toBeDisabled();

    resolve(tokenResponse());
    await waitFor(() => expect(getAccessToken()).toBe('jwt-login'));
  });

  it('requires both fields before it will submit (client-side guard)', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch');
    const user = userEvent.setup();
    renderWithQuery(<LoginScreen />);

    await user.click(screen.getByRole('button', { name: /sign in/i }));

    // Native required validation blocks the submit; no request goes out.
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});
