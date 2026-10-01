import { act } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  clearAccessToken,
  getAccessToken,
  getCurrentUser,
  installAuthRefresh,
  login,
  registerRefreshHandler,
  request,
  refresh,
  setAccessToken,
} from '@/api';
import { renderWithQuery } from '@/test/renderWithQuery';
import { useAuthStore } from './authStore';
import { currentUserQueryKey } from './queries';

const providerQueryKey = ['admin', 'llm-providers'] as const;
const documentQueryKey = ['documents', 'list'] as const;

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function unauthorized(): Response {
  return json({ type: 'about:blank', title: 'Unauthorized', status: 401 }, 401);
}

afterEach(() => {
  registerRefreshHandler(null);
  clearAccessToken();
  useAuthStore.setState({ status: 'unknown' });
  vi.restoreAllMocks();
});

describe('principal lifecycle', () => {
  it('clears every cache before a direct account/tenant token replacement renders', () => {
    act(() => {
      setAccessToken('jwt-persona-a');
      useAuthStore.setState({ status: 'authenticated' });
    });
    const { queryClient } = renderWithQuery(<div>account switch harness</div>);
    queryClient.setQueryData(currentUserQueryKey, { sentinel: 'persona-a-current-user' });
    queryClient.setQueryData(providerQueryKey, { sentinel: 'persona-a-provider' });
    queryClient.setQueryData(documentQueryKey, { sentinel: 'persona-a-document' });
    queryClient.getMutationCache().build(queryClient, {
      mutationKey: ['persona-a-mutation'],
      mutationFn: async () => undefined,
    });

    act(() => setAccessToken('jwt-persona-b', 'login'));

    expect(getAccessToken()).toBe('jwt-persona-b');
    expect(useAuthStore.getState().status).toBe('authenticated');
    expect(queryClient.getQueryCache().getAll()).toHaveLength(0);
    expect(queryClient.getMutationCache().getAll()).toHaveLength(0);
  });

  it('does not tear down same-principal data for a successful access-token refresh', async () => {
    act(() => {
      setAccessToken('jwt-persona-a');
      useAuthStore.setState({ status: 'authenticated' });
    });
    const { queryClient } = renderWithQuery(<div>refresh harness</div>);
    queryClient.setQueryData(documentQueryKey, { sentinel: 'same-persona-document' });

    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      json({ access_token: 'jwt-persona-a-refreshed', token_type: 'bearer', expires_in: 900 }),
    );
    installAuthRefresh();
    await act(async () => {
      await refresh();
    });

    expect(queryClient.getQueryData(documentQueryKey)).toEqual({
      sentinel: 'same-persona-document',
    });
    expect(useAuthStore.getState().status).toBe('authenticated');
  });

  it('fails closed across failed refresh, late A data, and a subsequent B login (R1-001)', async () => {
    const authorizations: Array<{ path: string; value: string | null }> = [];
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const path = new URL(String(input), window.location.origin).pathname;
      const authorization = new Headers(init?.headers).get('Authorization');
      authorizations.push({ path, value: authorization });

      if (path.endsWith('/auth/probe')) return unauthorized();
      if (path.endsWith('/auth/refresh')) return unauthorized();
      if (path.endsWith('/auth/login')) {
        return json({ access_token: 'jwt-persona-b', token_type: 'bearer', expires_in: 900 });
      }
      if (path.endsWith('/auth/me')) {
        return json({
          id: '00000000-0000-0000-0000-000000000002',
          tenant_id: '10000000-0000-0000-0000-000000000002',
          email: 'persona-b@example.test',
          roles: ['admin'],
          created_at: '2026-08-12T00:00:00Z',
        });
      }
      return json({ type: 'about:blank', title: 'Not found', status: 404 }, 404);
    });

    act(() => {
      setAccessToken('jwt-persona-a');
      useAuthStore.setState({ status: 'authenticated' });
    });
    const { queryClient } = renderWithQuery(<div>principal lifecycle harness</div>);

    queryClient.setQueryData(currentUserQueryKey, { sentinel: 'persona-a-current-user' });
    queryClient.setQueryData(providerQueryKey, { sentinel: 'persona-a-provider' });
    queryClient.setQueryData(documentQueryKey, { sentinel: 'persona-a-document' });
    queryClient.getMutationCache().build(queryClient, {
      mutationKey: ['persona-a-mutation'],
      mutationFn: async () => undefined,
    });

    let lateSignal: AbortSignal | undefined;
    let resolveLate!: (value: { sentinel: string }) => void;
    const lateQuery = queryClient
      .fetchQuery({
        queryKey: ['tenant', 'late-persona-a'],
        queryFn: ({ signal }) => {
          lateSignal = signal;
          return new Promise<{ sentinel: string }>((resolve) => {
            resolveLate = resolve;
          });
        },
      })
      .catch(() => undefined);

    installAuthRefresh();
    await expect(request('/auth/probe')).rejects.toEqual(expect.objectContaining({ status: 401 }));

    expect(getAccessToken()).toBeNull();
    expect(useAuthStore.getState().status).toBe('unauthenticated');
    expect(lateSignal?.aborted).toBe(true);
    expect(queryClient.getQueryCache().getAll()).toHaveLength(0);
    expect(queryClient.getMutationCache().getAll()).toHaveLength(0);

    resolveLate({ sentinel: 'late-persona-a-response' });
    await lateQuery;
    expect(queryClient.getQueryData(['tenant', 'late-persona-a'])).toBeUndefined();

    await login({ email: 'persona-b@example.test', password: 'persona-b-password' });
    const personaB = await getCurrentUser();

    expect(personaB.email).toBe('persona-b@example.test');
    expect(getAccessToken()).toBe('jwt-persona-b');
    expect(useAuthStore.getState().status).toBe('authenticated');
    expect(JSON.stringify(queryClient.getQueryCache().getAll())).not.toContain('persona-a');
    expect(authorizations.find(({ path }) => path.endsWith('/auth/me'))?.value).toBe(
      'Bearer jwt-persona-b',
    );
    expect(
      authorizations
        .filter(({ path }) => path.endsWith('/auth/refresh'))
        .every(({ value }) => value === null),
    ).toBe(true);
  });
});
