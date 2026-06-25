/**
 * Preferences query hooks (spec 0005, #144): usePreferences reads GET
 * /preferences; useUpdatePreferences PATCHes and primes the query cache from the
 * response. Drives the real api/ client against a mocked fetch.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, waitFor, act } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { setAccessToken, clearAccessToken } from '@/api';
import { usePreferences, useUpdatePreferences } from './queries';

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function makeWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 0 } },
  });
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  };
}

beforeEach(() => setAccessToken('jwt'));
afterEach(() => {
  clearAccessToken();
  vi.restoreAllMocks();
});

describe('preferences queries', () => {
  it('usePreferences fetches the caller preferences', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      json({ default_model_id: 'm1', updated_at: '2026-06-20T00:00:00Z' }),
    );
    const { result } = renderHook(() => usePreferences(), { wrapper: makeWrapper() });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.default_model_id).toBe('m1');
  });

  it('useUpdatePreferences PATCHes the default model and primes the cache', async () => {
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(json({ default_model_id: null, updated_at: null })) // initial GET
      .mockResolvedValueOnce(json({ default_model_id: 'm2', updated_at: '2026-06-20T00:00:00Z' }));
    const { result } = renderHook(
      () => ({ prefs: usePreferences(), update: useUpdatePreferences() }),
      { wrapper: makeWrapper() },
    );
    await waitFor(() => expect(result.current.prefs.isSuccess).toBe(true));

    await act(async () => {
      await result.current.update.mutateAsync({ default_model_id: 'm2' });
    });
    // The mutation primes the query cache from its response (no refetch needed).
    await waitFor(() => expect(result.current.prefs.data?.default_model_id).toBe('m2'));

    const patch = fetchSpy.mock.calls.find((c) => (c[1] as RequestInit)?.method === 'PATCH');
    expect(patch).toBeTruthy();
    expect(JSON.parse((patch?.[1] as RequestInit).body as string)).toMatchObject({
      default_model_id: 'm2',
    });
  });
});
