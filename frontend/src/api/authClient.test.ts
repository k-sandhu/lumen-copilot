/**
 * Auth-wiring behavior of the shared `request` client: bearer injection, the
 * credentials policy, and the single silent-refresh-then-retry on 401
 * (AC-1, AC-2, AC-4). The plain transport behavior lives in client.test.ts.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { request, registerRefreshHandler } from './client';
import {
  setAccessToken,
  setRefreshedAccessToken,
  clearAccessToken,
  getAccessToken,
  getPrincipalGeneration,
} from './token';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function problem(status: number): Response {
  return new Response(JSON.stringify({ type: 'about:blank', title: 'x', status }), {
    status,
    headers: { 'Content-Type': 'application/problem+json' },
  });
}

beforeEach(() => {
  clearAccessToken();
  registerRefreshHandler(null);
});
afterEach(() => vi.restoreAllMocks());

describe('bearer injection (AC-1)', () => {
  it('adds Authorization: Bearer when a token is held', async () => {
    setAccessToken('jwt-xyz');
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({ ok: true }));

    await request('/collections');

    const headers = new Headers((fetchSpy.mock.calls[0]?.[1] as RequestInit).headers);
    expect(headers.get('Authorization')).toBe('Bearer jwt-xyz');
  });

  it('omits Authorization when no token is held', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({ ok: true }));
    await request('/health');
    const headers = new Headers((fetchSpy.mock.calls[0]?.[1] as RequestInit).headers);
    expect(headers.has('Authorization')).toBe(false);
  });

  it('omits Authorization when skipAuth is set (login/refresh)', async () => {
    setAccessToken('jwt-xyz');
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({ ok: true }));
    await request('/auth/refresh', { method: 'POST', skipAuth: true });
    const headers = new Headers((fetchSpy.mock.calls[0]?.[1] as RequestInit).headers);
    expect(headers.has('Authorization')).toBe(false);
  });

  it('always sends credentials so the httpOnly refresh cookie rides along', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({ ok: true }));
    await request('/collections');
    expect((fetchSpy.mock.calls[0]?.[1] as RequestInit).credentials).toBe('include');
  });
});

describe('silent refresh-then-retry on 401 (AC-2, AC-4)', () => {
  it('refreshes, re-injects the new token, and retries once on 401', async () => {
    setAccessToken('expired');

    // 1st call → 401; after a successful refresh the retry → 200.
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(problem(401))
      .mockResolvedValueOnce(jsonResponse({ items: [] }));

    // The refresh handler installs a fresh token, mimicking auth.refresh().
    const handler = vi.fn(async () => {
      setRefreshedAccessToken('fresh', getPrincipalGeneration());
    });
    registerRefreshHandler(handler);

    const data = await request<{ items: unknown[] }>('/collections');

    expect(handler).toHaveBeenCalledTimes(1);
    expect(data.items).toEqual([]);
    // The retry carried the refreshed token.
    const retryHeaders = new Headers((fetchSpy.mock.calls[1]?.[1] as RequestInit).headers);
    expect(retryHeaders.get('Authorization')).toBe('Bearer fresh');
  });

  it('propagates the 401 when the refresh handler fails (→ caller routes to login)', async () => {
    setAccessToken('expired');
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(401));
    const handler = vi.fn(async () => {
      clearAccessToken();
      throw new Error('refresh failed');
    });
    registerRefreshHandler(handler);

    await expect(request('/collections')).rejects.toMatchObject({ status: 401 });
    expect(handler).toHaveBeenCalledTimes(1);
    expect(getAccessToken()).toBeNull();
  });

  it('does not attempt refresh when skipAuth is set (avoids /auth/refresh recursion)', async () => {
    setAccessToken('expired');
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(401));
    const handler = vi.fn(async () => {});
    registerRefreshHandler(handler);

    await expect(
      request('/auth/refresh', { method: 'POST', skipAuth: true }),
    ).rejects.toMatchObject({ status: 401 });
    expect(handler).not.toHaveBeenCalled();
  });

  it('only retries once — a 401 on the retry propagates', async () => {
    setAccessToken('expired');
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(problem(401));
    const handler = vi.fn(async () => {
      setRefreshedAccessToken('still-bad', getPrincipalGeneration());
    });
    registerRefreshHandler(handler);

    await expect(request('/collections')).rejects.toMatchObject({ status: 401 });
    // Original + one retry = exactly two fetches; refresh attempted once.
    expect(fetchSpy).toHaveBeenCalledTimes(2);
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it('does not return an old principal success that resolves after an account switch', async () => {
    setAccessToken('persona-a');
    let resolveRequest!: (response: Response) => void;
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockReturnValue(
      new Promise<Response>((resolve) => {
        resolveRequest = resolve;
      }),
    );

    const oldOutcome = request('/collections').then(
      () => null,
      (error: unknown) => error,
    );
    setAccessToken('persona-b', 'login');
    resolveRequest(jsonResponse({ sentinel: 'persona-a-late-success' }));

    await expect(oldOutcome).resolves.toMatchObject({ status: 401 });
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    expect(getAccessToken()).toBe('persona-b');
  });
});
