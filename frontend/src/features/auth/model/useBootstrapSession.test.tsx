/**
 * Boot-time silent refresh: on first load the app tries POST /auth/refresh to
 * re-establish a session from the httpOnly cookie. Success → authenticated;
 * failure → unauthenticated (route to login). This is what lets a page reload
 * keep the user signed in even though the access token is memory-only (AC-3).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { useBootstrapSession, resetBootstrapForTests } from './useBootstrapSession';
import { useAuthStore } from './authStore';
import { clearAccessToken, getAccessToken, installAuthRefresh, login } from '@/api';

function tokenResponse(): Response {
  return new Response(
    JSON.stringify({ access_token: 'jwt-boot', token_type: 'bearer', expires_in: 900 }),
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
  installAuthRefresh();
});
afterEach(() => vi.restoreAllMocks());

describe('useBootstrapSession', () => {
  it('marks authenticated when the refresh cookie is valid', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(tokenResponse());

    renderHook(() => useBootstrapSession());

    await waitFor(() => expect(useAuthStore.getState().status).toBe('authenticated'));
  });

  it('marks unauthenticated when there is no valid refresh cookie', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(unauthorized());

    renderHook(() => useBootstrapSession());

    await waitFor(() => expect(useAuthStore.getState().status).toBe('unauthenticated'));
  });

  it('attempts the refresh exactly once', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(tokenResponse());

    const { rerender } = renderHook(() => useBootstrapSession());
    await waitFor(() => expect(useAuthStore.getState().status).toBe('authenticated'));
    rerender();

    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });

  it.each(['success', 'failure'] as const)(
    'cannot let a late bootstrap refresh %s overwrite or demote a newer login (R2-005)',
    async (bootstrapOutcome) => {
      let resolveBootstrap!: (response: Response) => void;
      let markBootstrapStarted!: () => void;
      const bootstrapStarted = new Promise<void>((resolve) => {
        markBootstrapStarted = resolve;
      });
      let refreshCalls = 0;
      let loginCalls = 0;

      vi.spyOn(globalThis, 'fetch').mockImplementation((input) => {
        const path = new URL(String(input), window.location.origin).pathname;
        if (path.endsWith('/auth/refresh')) {
          refreshCalls += 1;
          markBootstrapStarted();
          return new Promise<Response>((resolve) => {
            resolveBootstrap = resolve;
          });
        }
        if (path.endsWith('/auth/login')) {
          loginCalls += 1;
          return Promise.resolve(
            new Response(
              JSON.stringify({
                access_token: 'jwt-persona-b',
                token_type: 'bearer',
                expires_in: 900,
              }),
              { status: 200, headers: { 'Content-Type': 'application/json' } },
            ),
          );
        }
        return Promise.resolve(unauthorized());
      });

      renderHook(() => useBootstrapSession());
      await bootstrapStarted;
      await login({ email: 'persona-b@example.test', password: 'persona-b-password' });

      if (bootstrapOutcome === 'success') resolveBootstrap(tokenResponse());
      else resolveBootstrap(unauthorized());

      await waitFor(() => expect(useAuthStore.getState().status).toBe('authenticated'));
      expect(getAccessToken()).toBe('jwt-persona-b');
      expect(refreshCalls).toBe(1);
      expect(loginCalls).toBe(1);
    },
  );
});
