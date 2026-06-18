/**
 * RouteGuard (AC-3): unauthenticated users see the login screen; authenticated
 * users see the app shell (children). While the session is bootstrapping
 * (silent refresh in flight) it shows a loading state — never a login flash.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { renderWithQuery } from '@/test/renderWithQuery';
import { RouteGuard } from './RouteGuard';
import { useAuthStore } from '../model/authStore';
import { resetBootstrapForTests } from '../model/useBootstrapSession';
import { clearAccessToken } from '@/api';

function tokenResponse(): Response {
  return new Response(
    JSON.stringify({ access_token: 'jwt', token_type: 'bearer', expires_in: 900 }),
    { status: 200, headers: { 'Content-Type': 'application/json' } },
  );
}

function unauthorized(): Response {
  return new Response(JSON.stringify({ type: 'about:blank', title: 'x', status: 401 }), {
    status: 401,
    headers: { 'Content-Type': 'application/problem+json' },
  });
}

beforeEach(() => {
  clearAccessToken();
  useAuthStore.setState({ status: 'unknown' });
  resetBootstrapForTests();
});
afterEach(() => vi.restoreAllMocks());

describe('RouteGuard', () => {
  it('shows a loading state while bootstrapping (no login flash) (AC-3)', () => {
    // Pending refresh — never resolves during this assertion.
    vi.spyOn(globalThis, 'fetch').mockReturnValue(new Promise<Response>(() => {}));
    renderWithQuery(
      <RouteGuard>
        <div>protected shell</div>
      </RouteGuard>,
    );
    expect(screen.getByRole('status')).toBeInTheDocument();
    expect(screen.queryByText('protected shell')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /sign in/i })).not.toBeInTheDocument();
  });

  it('routes unauthenticated users to the login screen (AC-3)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(unauthorized());
    renderWithQuery(
      <RouteGuard>
        <div>protected shell</div>
      </RouteGuard>,
    );
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /sign in/i })).toBeInTheDocument(),
    );
    expect(screen.queryByText('protected shell')).not.toBeInTheDocument();
  });

  it('renders the app shell once authenticated (AC-3)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(tokenResponse());
    renderWithQuery(
      <RouteGuard>
        <div>protected shell</div>
      </RouteGuard>,
    );
    await waitFor(() => expect(screen.getByText('protected shell')).toBeInTheDocument());
  });
});
