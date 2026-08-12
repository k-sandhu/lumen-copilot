/**
 * CurrentUserMenu (AC-2): GET /auth/me populates the current-user UI; a logout
 * control calls POST /auth/logout and clears state. Covers loading + loaded +
 * the logout interaction.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithQuery } from '@/test/renderWithQuery';
import { CurrentUserMenu } from './CurrentUserMenu';
import { useAuthStore } from '../model/authStore';
import { setAccessToken, clearAccessToken, getAccessToken } from '@/api';

const ME = {
  id: '11111111-1111-1111-1111-111111111111',
  email: 'kw@acme.test',
  tenant_id: '22222222-2222-2222-2222-222222222222',
  roles: ['member'],
  created_at: '2026-06-18T00:00:00Z',
};

function meResponse(): Response {
  return new Response(JSON.stringify(ME), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

function noContent(): Response {
  return new Response(null, { status: 204 });
}

beforeEach(() => {
  setAccessToken('jwt');
  useAuthStore.setState({ status: 'authenticated' });
});
afterEach(() => {
  vi.restoreAllMocks();
  clearAccessToken();
});

describe('CurrentUserMenu', () => {
  it('shows the signed-in user from GET /auth/me (AC-2)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(meResponse());
    renderWithQuery(<CurrentUserMenu />);
    expect(await screen.findByText('kw@acme.test')).toBeInTheDocument();
  });

  it('tears down at logout intent while revocation is delayed and cannot clear a later login (R1-002)', async () => {
    let resolveLogout!: (response: Response) => void;
    let logoutInit: RequestInit | undefined;
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
      const url = String(input);
      if (url.includes('/auth/logout')) {
        logoutInit = init;
        return new Promise<Response>((resolve) => {
          resolveLogout = resolve;
        });
      }
      return Promise.resolve(meResponse());
    });
    const user = userEvent.setup();
    const { queryClient } = renderWithQuery(<CurrentUserMenu />);

    await screen.findByText('kw@acme.test');
    queryClient.setQueryData(['tenant', 'persona-a-sentinel'], 'persona-a-data');
    queryClient.getMutationCache().build(queryClient, {
      mutationKey: ['persona-a-mutation'],
      mutationFn: async () => undefined,
    });
    await user.click(screen.getByRole('button', { name: /sign out/i }));

    // The local boundary is synchronous with intent; it cannot wait for the
    // best-effort revocation request, which is deliberately still unresolved.
    expect(getAccessToken()).toBeNull();
    expect(useAuthStore.getState().status).toBe('unauthenticated');
    expect(
      queryClient
        .getQueryCache()
        .getAll()
        .every((query) => query.state.data === undefined),
    ).toBe(true);
    expect(JSON.stringify(queryClient.getQueryCache().getAll())).not.toContain('persona-a-data');
    expect(queryClient.getMutationCache().getAll()).toHaveLength(0);

    const logoutCall = fetchSpy.mock.calls.find((c) => String(c[0]).includes('/auth/logout'));
    expect(logoutCall).toBeDefined();
    expect(logoutInit?.method).toBe('POST');
    expect(new Headers(logoutInit?.headers).get('Authorization')).toBe('Bearer jwt');

    // A second principal may sign in while A's revocation is still in flight.
    // Its token/cache must survive A's late success.
    act(() => setAccessToken('jwt-persona-b'));
    queryClient.setQueryData(['tenant', 'persona-b-sentinel'], 'persona-b-data');
    resolveLogout(noContent());

    await waitFor(() =>
      expect(queryClient.getQueryData(['tenant', 'persona-b-sentinel'])).toBe('persona-b-data'),
    );
    expect(getAccessToken()).toBe('jwt-persona-b');
    expect(useAuthStore.getState().status).toBe('authenticated');
  });
});
