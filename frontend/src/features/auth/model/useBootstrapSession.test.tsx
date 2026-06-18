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
import { clearAccessToken } from '@/api';

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
});
