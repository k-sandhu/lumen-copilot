import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { login, refresh, getCurrentUser, logout } from './auth';
import { ApiError } from './client';
import { getAccessToken, setAccessToken, clearAccessToken } from './token';

function jsonResponse(body: unknown, status = 200, contentType = 'application/json'): Response {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { 'Content-Type': contentType },
  });
}

function problemResponse(status: number, body: Partial<Record<string, unknown>> = {}): Response {
  return new Response(JSON.stringify({ type: 'about:blank', title: 'Error', status, ...body }), {
    status,
    headers: { 'Content-Type': 'application/problem+json' },
  });
}

beforeEach(() => clearAccessToken());
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('login', () => {
  it('exchanges credentials for a token and stores it (AC-1)', async () => {
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(
        jsonResponse({ access_token: 'jwt-abc', token_type: 'bearer', expires_in: 900 }),
      );

    const token = await login({ email: 'kw@acme.test', password: 'pw' });

    expect(token.access_token).toBe('jwt-abc');
    expect(getAccessToken()).toBe('jwt-abc');

    // The request includes credentials so the httpOnly refresh cookie is set.
    const init = fetchSpy.mock.calls[0]?.[1] as RequestInit;
    expect(init.method).toBe('POST');
    expect(init.credentials).toBe('include');
  });

  it('does NOT send a bearer header (login is unauthenticated)', async () => {
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(
        jsonResponse({ access_token: 'jwt-abc', token_type: 'bearer', expires_in: 900 }),
      );
    await login({ email: 'kw@acme.test', password: 'pw' });
    const headers = new Headers((fetchSpy.mock.calls[0]?.[1] as RequestInit).headers);
    expect(headers.has('Authorization')).toBe(false);
  });

  it('surfaces a 401 as an ApiError without storing a token (AC-4 bad creds)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      problemResponse(401, { title: 'Unauthorized', detail: 'Invalid email or password.' }),
    );

    await expect(login({ email: 'nope@acme.test', password: 'bad' })).rejects.toMatchObject({
      name: 'ApiError',
      status: 401,
    });
    expect(getAccessToken()).toBeNull();
  });
});

describe('refresh', () => {
  it('mints and stores a new token from the refresh cookie (AC-2)', async () => {
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(
        jsonResponse({ access_token: 'jwt-new', token_type: 'bearer', expires_in: 900 }),
      );

    const token = await refresh();

    expect(token.access_token).toBe('jwt-new');
    expect(getAccessToken()).toBe('jwt-new');
    const init = fetchSpy.mock.calls[0]?.[1] as RequestInit;
    expect(init.credentials).toBe('include');
  });

  it('rejects (failed refresh) and leaves no token', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(problemResponse(401));
    await expect(refresh()).rejects.toBeInstanceOf(ApiError);
    expect(getAccessToken()).toBeNull();
  });
});

describe('getCurrentUser', () => {
  it('returns the principal (AC-2 current user)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({
        id: '11111111-1111-1111-1111-111111111111',
        email: 'kw@acme.test',
        tenant_id: '22222222-2222-2222-2222-222222222222',
        roles: ['member'],
        created_at: '2026-06-18T00:00:00Z',
      }),
    );

    const me = await getCurrentUser();
    expect(me.email).toBe('kw@acme.test');
    expect(me.roles).toContain('member');
  });
});

describe('logout', () => {
  it('calls the endpoint and clears the in-memory token (AC-2)', async () => {
    const { setAccessToken } = await import('./token');
    setAccessToken('jwt-abc');
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse(null, 204));

    await logout();

    expect(getAccessToken()).toBeNull();
    const init = fetchSpy.mock.calls[0]?.[1] as RequestInit;
    expect(init.method).toBe('POST');
    expect(init.credentials).toBe('include');
  });

  it('clears the token even if the server call fails (defensive)', async () => {
    const { setAccessToken } = await import('./token');
    setAccessToken('jwt-abc');
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new TypeError('Failed to fetch'));

    await logout();

    expect(getAccessToken()).toBeNull();
  });

  it('settles A logout before dispatching B login so B cookie/token wins (R1-002/R1-005)', async () => {
    setAccessToken('jwt-persona-a');
    let resolveLogout!: (response: Response) => void;
    let markLogoutStarted!: () => void;
    const logoutStarted = new Promise<void>((resolve) => {
      markLogoutStarted = resolve;
    });
    const order: string[] = [];
    let loginCalls = 0;

    vi.spyOn(globalThis, 'fetch').mockImplementation((input) => {
      const path = new URL(String(input), window.location.origin).pathname;
      if (path.endsWith('/auth/logout')) {
        order.push('logout-start');
        markLogoutStarted();
        return new Promise<Response>((resolve) => {
          resolveLogout = (response) => {
            order.push('logout-settled');
            resolve(response);
          };
        });
      }
      if (path.endsWith('/auth/login')) {
        loginCalls += 1;
        order.push('login-start');
        return Promise.resolve(
          jsonResponse({ access_token: 'jwt-persona-b', token_type: 'bearer', expires_in: 900 }),
        );
      }
      return Promise.resolve(problemResponse(404));
    });

    const personaALogout = logout();
    await logoutStarted;
    const personaBLogin = login({
      email: 'persona-b@example.test',
      password: 'persona-b-password',
    });
    await Promise.resolve();
    await Promise.resolve();

    expect(loginCalls).toBe(0);
    resolveLogout(jsonResponse(null, 204));
    await personaALogout;
    await personaBLogin;

    expect(order).toEqual(['logout-start', 'logout-settled', 'login-start']);
    expect(getAccessToken()).toBe('jwt-persona-b');
  });

  it('aborts a hung A logout after the bounded barrier and allows B login (R1-002)', async () => {
    vi.useFakeTimers();
    setAccessToken('jwt-persona-a');
    let logoutSignal: AbortSignal | undefined;
    let markLogoutStarted!: () => void;
    const logoutStarted = new Promise<void>((resolve) => {
      markLogoutStarted = resolve;
    });
    let loginCalls = 0;

    vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
      const path = new URL(String(input), window.location.origin).pathname;
      if (path.endsWith('/auth/logout')) {
        logoutSignal = init?.signal ?? undefined;
        markLogoutStarted();
        // Ignore abort to prove the coordinator itself does not await an
        // uncooperative transport forever.
        return new Promise<Response>(() => {});
      }
      if (path.endsWith('/auth/login')) {
        loginCalls += 1;
        return Promise.resolve(
          jsonResponse({ access_token: 'jwt-persona-b', token_type: 'bearer', expires_in: 900 }),
        );
      }
      return Promise.resolve(problemResponse(404));
    });

    void logout();
    await logoutStarted;
    const personaBLogin = login({
      email: 'persona-b@example.test',
      password: 'persona-b-password',
    });
    await Promise.resolve();
    expect(loginCalls).toBe(0);

    await vi.advanceTimersByTimeAsync(1_500);
    await personaBLogin;

    expect(logoutSignal?.aborted).toBe(true);
    expect(loginCalls).toBe(1);
    expect(getAccessToken()).toBe('jwt-persona-b');
  });
});
