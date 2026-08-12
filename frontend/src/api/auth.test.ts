import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { login, refresh, getCurrentUser, installAuthRefresh, logout } from './auth';
import { ApiError } from './client';
import { setActiveAuthSlot } from './authSlot';
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

beforeEach(() => {
  localStorage.clear();
  clearAccessToken();
  installAuthRefresh();
});
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
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(
        problemResponse(401, { title: 'Unauthorized', detail: 'Invalid email or password.' }),
      );

    await expect(login({ email: 'nope@acme.test', password: 'bad' })).rejects.toMatchObject({
      name: 'ApiError',
      status: 401,
    });
    expect(getAccessToken()).toBeNull();
    expect(fetchSpy).toHaveBeenCalledOnce();
  });

  it('cleans up a slot whose Set-Cookie may have landed before a transport failure', async () => {
    const paths: string[] = [];
    vi.spyOn(globalThis, 'fetch').mockImplementation((input) => {
      const path = new URL(String(input), window.location.origin).pathname;
      paths.push(path);
      if (path.endsWith('/auth/login')) return Promise.reject(new TypeError('connection reset'));
      if (path.endsWith('/auth/refresh')) {
        return Promise.resolve(
          jsonResponse({
            access_token: 'cleanup-only-bearer',
            token_type: 'bearer',
            expires_in: 900,
          }),
        );
      }
      if (path.endsWith('/auth/logout')) return Promise.resolve(jsonResponse(null, 204));
      return Promise.resolve(problemResponse(404));
    });

    await expect(
      login({ email: 'persona-a@example.test', password: 'persona-a-password' }),
    ).rejects.toMatchObject({ status: 0 });
    await vi.waitFor(() =>
      expect(paths).toEqual(['/api/v1/auth/login', '/api/v1/auth/refresh', '/api/v1/auth/logout']),
    );
    expect(getAccessToken()).toBeNull();
  });

  it.each(['older-first', 'newer-first'] as const)(
    'makes the newest concurrent login intent authoritative when %s resolves',
    async (responseOrder) => {
      let resolveA!: (response: Response) => void;
      let resolveB!: (response: Response) => void;
      const calls: Array<{ email: string; slot: string | null; signal: AbortSignal | null }> = [];

      vi.spyOn(globalThis, 'fetch').mockImplementation((_input, init) => {
        const body = JSON.parse(String(init?.body)) as { email: string };
        const call = {
          email: body.email,
          slot: new Headers(init?.headers).get('X-Lumen-Auth-Slot'),
          signal: init?.signal ?? null,
        };
        calls.push(call);
        return new Promise<Response>((resolve) => {
          if (body.email.startsWith('persona-a')) resolveA = resolve;
          else resolveB = resolve;
        });
      });

      const older = login({
        email: 'persona-a@example.test',
        password: 'persona-a-password',
      });
      const newer = login({
        email: 'persona-b@example.test',
        password: 'persona-b-password',
      });
      await vi.waitFor(() => expect(calls).toHaveLength(2));

      const responseA = jsonResponse({
        access_token: 'jwt-persona-a',
        token_type: 'bearer',
        expires_in: 900,
      });
      const responseB = jsonResponse({
        access_token: 'jwt-persona-b',
        token_type: 'bearer',
        expires_in: 900,
      });
      if (responseOrder === 'older-first') {
        resolveA(responseA);
        await Promise.resolve();
        resolveB(responseB);
      } else {
        resolveB(responseB);
        await newer;
        resolveA(responseA);
      }

      await expect(older).rejects.toThrow(/discarded|superseded|abort/i);
      await expect(newer).resolves.toMatchObject({ access_token: 'jwt-persona-b' });
      expect(getAccessToken()).toBe('jwt-persona-b');
      // Supersession aborts transport eagerly, while the generation guard and
      // exact-slot cleanup still handle a server that accepted it already.
      expect(calls[0]?.signal?.aborted).toBe(true);
      expect(calls[0]?.slot).toMatch(/^[0-9a-f-]{36}$/i);
      expect(calls[1]?.slot).toMatch(/^[0-9a-f-]{36}$/i);
      expect(calls[0]?.slot).not.toBe(calls[1]?.slot);
    },
  );

  it('does not resurrect an older successful login when the newer credentials fail', async () => {
    let resolveOlder!: (response: Response) => void;
    let resolveNewer!: (response: Response) => void;

    vi.spyOn(globalThis, 'fetch').mockImplementation((_input, init) => {
      const body = JSON.parse(String(init?.body)) as { email: string };
      return new Promise<Response>((resolve) => {
        if (body.email.startsWith('persona-a')) resolveOlder = resolve;
        else resolveNewer = resolve;
      });
    });

    const older = login({
      email: 'persona-a@example.test',
      password: 'persona-a-password',
    });
    const newer = login({
      email: 'persona-b@example.test',
      password: 'wrong-password',
    });
    await vi.waitFor(() => {
      expect(resolveOlder).toBeTypeOf('function');
      expect(resolveNewer).toBeTypeOf('function');
    });

    resolveNewer(problemResponse(401, { detail: 'Invalid email or password.' }));
    await expect(newer).rejects.toMatchObject({ status: 401 });
    resolveOlder(
      jsonResponse({ access_token: 'jwt-persona-a', token_type: 'bearer', expires_in: 900 }),
    );

    await expect(older).rejects.toThrow(/discarded|superseded|abort/i);
    expect(getAccessToken()).toBeNull();
  });

  it('preserves an existing authenticated principal when a direct newer login fails', async () => {
    setAccessToken('jwt-existing-persona');
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      problemResponse(401, { detail: 'Invalid email or password.' }),
    );

    await expect(
      login({ email: 'persona-b@example.test', password: 'wrong-password' }),
    ).rejects.toMatchObject({ status: 401 });

    expect(getAccessToken()).toBe('jwt-existing-persona');
  });

  it('cancels caller observation without letting an accepted late login authenticate', async () => {
    const controller = new AbortController();
    let resolveLogin!: (response: Response) => void;
    const requests: string[] = [];
    vi.spyOn(globalThis, 'fetch').mockImplementation((input) => {
      const path = new URL(String(input), window.location.origin).pathname;
      requests.push(path);
      if (path.endsWith('/auth/login')) {
        return new Promise<Response>((resolve) => {
          resolveLogin = resolve;
        });
      }
      if (path.endsWith('/auth/logout')) return Promise.resolve(jsonResponse(null, 204));
      return Promise.resolve(problemResponse(404));
    });

    const outcome = login(
      { email: 'persona-a@example.test', password: 'persona-a-password' },
      controller.signal,
    );
    await vi.waitFor(() => expect(resolveLogin).toBeTypeOf('function'));
    controller.abort();
    await expect(outcome).rejects.toMatchObject({ name: 'AbortError' });

    // Headers may already have been accepted, so the transport is allowed to
    // finish and its distinct server session is explicitly revoked.
    resolveLogin(
      jsonResponse({ access_token: 'jwt-persona-a', token_type: 'bearer', expires_in: 900 }),
    );
    await vi.waitFor(() =>
      expect(requests.filter((path) => path.endsWith('/auth/logout'))).toHaveLength(1),
    );
    expect(getAccessToken()).toBeNull();
  });

  it('aborts pre-token login when another tab selects a newer principal (R3-002)', async () => {
    const slotB = '22222222-2222-4222-8222-222222222222';
    let resolveLogin!: (response: Response) => void;
    let loginSignal: AbortSignal | null = null;
    const refreshSlots: Array<string | null> = [];

    vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
      const path = new URL(String(input), window.location.origin).pathname;
      const slot = new Headers(init?.headers).get('X-Lumen-Auth-Slot');
      if (path.endsWith('/auth/login')) {
        loginSignal = init?.signal ?? null;
        return new Promise<Response>((resolve) => {
          resolveLogin = resolve;
        });
      }
      if (path.endsWith('/auth/refresh')) {
        refreshSlots.push(slot);
        if (slot === slotB) {
          return Promise.resolve(
            jsonResponse({ access_token: 'jwt-persona-b', token_type: 'bearer', expires_in: 900 }),
          );
        }
        return Promise.resolve(problemResponse(401));
      }
      if (path.endsWith('/auth/logout')) return Promise.resolve(jsonResponse(null, 204));
      return Promise.resolve(problemResponse(404));
    });

    const pendingA = login({
      email: 'persona-a@example.test',
      password: 'persona-a-password',
    });
    await vi.waitFor(() => expect(resolveLogin).toBeTypeOf('function'));

    localStorage.setItem('lumen.active-auth-slot', slotB);
    window.dispatchEvent(
      new StorageEvent('storage', {
        key: 'lumen.active-auth-slot',
        oldValue: null,
        newValue: slotB,
        storageArea: localStorage,
      }),
    );
    expect((loginSignal as unknown as AbortSignal).aborted).toBe(true);

    resolveLogin(
      jsonResponse({ access_token: 'jwt-persona-a', token_type: 'bearer', expires_in: 900 }),
    );
    await expect(pendingA).rejects.toThrow(/abort|discarded|superseded/i);
    await vi.waitFor(() => expect(getAccessToken()).toBe('jwt-persona-b'));
    expect(localStorage.getItem('lumen.active-auth-slot')).toBe(slotB);
    expect(refreshSlots).toContain(slotB);
  });

  it('does not dispatch credentials when the login signal is already aborted', async () => {
    const controller = new AbortController();
    controller.abort();
    const fetchSpy = vi.spyOn(globalThis, 'fetch');

    await expect(
      login({ email: 'persona-a@example.test', password: 'persona-a-password' }, controller.signal),
    ).rejects.toMatchObject({ name: 'AbortError' });

    expect(fetchSpy).not.toHaveBeenCalled();
    expect(getAccessToken()).toBeNull();
  });

  it('makes the final intent authoritative across three repeated submissions', async () => {
    const resolvers = new Map<string, (response: Response) => void>();
    const loginSlots = new Map<string, string | null>();
    const retiredSlots: Array<string | null> = [];
    vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
      const path = new URL(String(input), window.location.origin).pathname;
      if (path.endsWith('/auth/logout')) {
        retiredSlots.push(new Headers(init?.headers).get('X-Lumen-Auth-Slot'));
        return Promise.resolve(jsonResponse(null, 204));
      }
      const email = (JSON.parse(String(init?.body)) as { email: string }).email;
      loginSlots.set(email, new Headers(init?.headers).get('X-Lumen-Auth-Slot'));
      return new Promise<Response>((resolve) => resolvers.set(email, resolve));
    });
    const attempts = ['a', 'b', 'c'].map((persona) =>
      login({ email: `persona-${persona}@example.test`, password: 'password' }),
    );
    await vi.waitFor(() => expect(resolvers.size).toBe(3));
    for (const persona of ['b', 'a', 'c']) {
      resolvers.get(`persona-${persona}@example.test`)?.(
        jsonResponse({
          access_token: `jwt-persona-${persona}`,
          token_type: 'bearer',
          expires_in: 900,
        }),
      );
    }
    await expect(attempts[0]).rejects.toThrow(/discarded|superseded/i);
    await expect(attempts[1]).rejects.toThrow(/discarded|superseded/i);
    await expect(attempts[2]).resolves.toMatchObject({ access_token: 'jwt-persona-c' });
    await vi.waitFor(() => expect(retiredSlots).toHaveLength(2));
    expect(new Set(retiredSlots)).toEqual(
      new Set([loginSlots.get('persona-a@example.test'), loginSlots.get('persona-b@example.test')]),
    );
    expect(getAccessToken()).toBe('jwt-persona-c');
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

  it('deduplicates concurrent direct/bootstrap refresh callers through one coordinator', async () => {
    let resolveRefresh!: (response: Response) => void;
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockReturnValue(
      new Promise<Response>((resolve) => {
        resolveRefresh = resolve;
      }),
    );

    const first = refresh();
    const second = refresh();
    await vi.waitFor(() => expect(fetchSpy).toHaveBeenCalledOnce());
    resolveRefresh(
      jsonResponse({ access_token: 'jwt-shared', token_type: 'bearer', expires_in: 900 }),
    );

    await expect(first).resolves.toMatchObject({ access_token: 'jwt-shared' });
    await expect(second).resolves.toMatchObject({ access_token: 'jwt-shared' });
    expect(getAccessToken()).toBe('jwt-shared');
  });

  it('recovers a losing cross-tab rotation when Web Locks is unavailable (R3-001)', async () => {
    const slot = 'abababab-abab-4bab-8bab-abababababab';
    setActiveAuthSlot(slot);
    const originalLocks = Object.getOwnPropertyDescriptor(navigator, 'locks');
    Object.defineProperty(navigator, 'locks', { configurable: true, value: undefined });
    let calls = 0;
    const revisionKey = 'lumen.auth-refresh-revision';

    vi.spyOn(globalThis, 'fetch').mockImplementation(() => {
      calls += 1;
      if (calls === 1) {
        const oldValue = localStorage.getItem(revisionKey);
        const newValue = JSON.stringify({
          slot,
          revision: 'cdcdcdcd-cdcd-4dcd-8dcd-cdcdcdcdcdcd',
        });
        queueMicrotask(() => {
          localStorage.setItem(revisionKey, newValue);
          window.dispatchEvent(
            new StorageEvent('storage', {
              key: revisionKey,
              oldValue,
              newValue,
              storageArea: localStorage,
            }),
          );
        });
        return Promise.resolve(problemResponse(401, { code: 'refresh_superseded' }));
      }
      return Promise.resolve(
        jsonResponse({ access_token: 'jwt-after-winner', token_type: 'bearer', expires_in: 900 }),
      );
    });

    try {
      await expect(refresh()).resolves.toMatchObject({ access_token: 'jwt-after-winner' });
    } finally {
      if (originalLocks) Object.defineProperty(navigator, 'locks', originalLocks);
      else Reflect.deleteProperty(navigator, 'locks');
    }
    expect(calls).toBe(2);
    expect(getAccessToken()).toBe('jwt-after-winner');
    expect(localStorage.getItem('lumen.active-auth-slot')).toBe(slot);
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
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation((input) => {
      const path = new URL(String(input), window.location.origin).pathname;
      if (path.endsWith('/auth/refresh')) {
        return Promise.resolve(
          jsonResponse({ access_token: 'jwt-persona-b', token_type: 'bearer', expires_in: 900 }),
        );
      }
      return Promise.resolve(jsonResponse(null, 204));
    });

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

  it('revokes an old tab without clearing a newer tab auth-slot selection', async () => {
    const slotA = '11111111-1111-4111-8111-111111111111';
    const slotB = '22222222-2222-4222-8222-222222222222';
    setAccessToken('jwt-persona-a', 'login', slotA);
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation((input) => {
      const path = new URL(String(input), window.location.origin).pathname;
      if (path.endsWith('/auth/refresh')) {
        return Promise.resolve(
          jsonResponse({ access_token: 'jwt-persona-b', token_type: 'bearer', expires_in: 900 }),
        );
      }
      return Promise.resolve(jsonResponse(null, 204));
    });

    localStorage.setItem('lumen.active-auth-slot', slotB);
    window.dispatchEvent(
      new StorageEvent('storage', {
        key: 'lumen.active-auth-slot',
        oldValue: slotA,
        newValue: slotB,
        storageArea: localStorage,
      }),
    );
    expect(getAccessToken()).toBeNull();
    expect(localStorage.getItem('lumen.active-auth-slot')).toBe(slotB);

    await vi.waitFor(() => expect(fetchSpy).toHaveBeenCalledTimes(2));
    const logoutCall = fetchSpy.mock.calls.find(([input]) =>
      new URL(String(input), window.location.origin).pathname.endsWith('/auth/logout'),
    );
    const headers = new Headers((logoutCall?.[1] as RequestInit).headers);
    expect(headers.get('Authorization')).toBe('Bearer jwt-persona-a');
    expect(headers.get('X-Lumen-Auth-Slot')).toBe(slotA);
    await vi.waitFor(() => expect(getAccessToken()).toBe('jwt-persona-b'));
  });

  it('never attaches a lagging bearer to a newer previous-slot cleanup hint', async () => {
    const slotA = '31313131-3131-4131-8131-313131313131';
    const slotB = '32323232-3232-4232-8232-323232323232';
    const slotC = '33333333-3333-4333-8333-333333333333';
    setAccessToken('jwt-persona-a', 'login', slotA);
    const calls: Array<{ path: string; bearer: string | null; slot: string | null }> = [];
    vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
      const path = new URL(String(input), window.location.origin).pathname;
      const headers = new Headers(init?.headers);
      calls.push({
        path,
        bearer: headers.get('Authorization'),
        slot: headers.get('X-Lumen-Auth-Slot'),
      });
      return Promise.resolve(problemResponse(401));
    });

    // Model storage advancing A -> B -> C before this tab completed B's
    // bootstrap. Its bearer still belongs to A, while oldValue now names B.
    localStorage.setItem('lumen.active-auth-slot', slotC);
    window.dispatchEvent(
      new StorageEvent('storage', {
        key: 'lumen.active-auth-slot',
        oldValue: slotB,
        newValue: slotC,
        storageArea: localStorage,
      }),
    );

    await vi.waitFor(() =>
      expect(calls.some((call) => call.path.endsWith('/auth/refresh') && call.slot === slotB)).toBe(
        true,
      ),
    );
    expect(
      calls.some(
        (call) =>
          call.path.endsWith('/auth/logout') &&
          call.slot === slotB &&
          call.bearer === 'Bearer jwt-persona-a',
      ),
    ).toBe(false);
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

  it('registers the whole logout transition before awaiting a held refresh (R2-002)', async () => {
    vi.useFakeTimers();
    setAccessToken('jwt-persona-a');
    let markRefreshStarted!: () => void;
    const refreshStarted = new Promise<void>((resolve) => {
      markRefreshStarted = resolve;
    });
    const order: string[] = [];

    vi.spyOn(globalThis, 'fetch').mockImplementation((input) => {
      const path = new URL(String(input), window.location.origin).pathname;
      if (path.endsWith('/probe')) return Promise.resolve(problemResponse(401));
      if (path.endsWith('/auth/refresh')) {
        order.push('refresh-start');
        markRefreshStarted();
        return new Promise<Response>(() => {});
      }
      if (path.endsWith('/auth/logout')) {
        order.push('logout-start');
        return Promise.resolve(jsonResponse(null, 204));
      }
      if (path.endsWith('/auth/login')) {
        order.push('login-start');
        return Promise.resolve(
          jsonResponse({ access_token: 'jwt-persona-b', token_type: 'bearer', expires_in: 900 }),
        );
      }
      return Promise.resolve(problemResponse(404));
    });

    const { installAuthRefresh, request } = await import('./index');
    installAuthRefresh();
    void request('/probe').catch(() => undefined);
    await refreshStarted;

    const outgoing = logout();
    const incoming = login({
      email: 'persona-b@example.test',
      password: 'persona-b-password',
    });
    await Promise.resolve();
    expect(order).toEqual(['refresh-start']);

    await vi.advanceTimersByTimeAsync(1_500);
    await outgoing;
    await incoming;

    expect(order).toEqual(['refresh-start', 'logout-start', 'login-start']);
    expect(getAccessToken()).toBe('jwt-persona-b');
  });

  it('supersedes a held login immediately when logout is the newest auth intent', async () => {
    let resolveLogin!: (response: Response) => void;
    vi.spyOn(globalThis, 'fetch').mockImplementation((input) => {
      const path = new URL(String(input), window.location.origin).pathname;
      if (path.endsWith('/auth/login')) {
        return new Promise<Response>((resolve) => {
          resolveLogin = resolve;
        });
      }
      if (path.endsWith('/auth/logout')) return Promise.resolve(jsonResponse(null, 204));
      return Promise.resolve(problemResponse(404));
    });

    const pendingLogin = login({
      email: 'persona-a@example.test',
      password: 'persona-a-password',
    });
    await Promise.resolve();
    await logout();
    resolveLogin(
      jsonResponse({ access_token: 'jwt-persona-a', token_type: 'bearer', expires_in: 900 }),
    );

    await expect(pendingLogin).rejects.toThrow(/discarded|superseded|abort/i);
    expect(getAccessToken()).toBeNull();
  });
});
