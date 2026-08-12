import { expect, test, type Page, type Route } from '@playwright/test';

type Persona = 'a' | 'b';

interface RequestRecord {
  authorization: string | null;
  body: unknown;
  method: string;
  path: string;
}

interface ApiHarnessOptions {
  delayLogout?: boolean;
  delayProviderCreate?: boolean;
  failGroupsForPersonaA?: boolean;
}

function deferred<T = void>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function personaFromBearer(authorization: string | null): Persona | null {
  if (authorization === 'Bearer jwt-persona-a') return 'a';
  if (authorization === 'Bearer jwt-persona-b') return 'b';
  return null;
}

function currentUser(persona: Persona) {
  return {
    id: `00000000-0000-0000-0000-00000000000${persona === 'a' ? '1' : '2'}`,
    tenant_id: `10000000-0000-0000-0000-00000000000${persona === 'a' ? '1' : '2'}`,
    tenant_name: `Persona ${persona.toUpperCase()} tenant`,
    email: `persona-${persona}@example.test`,
    roles: ['admin'],
    created_at: '2026-08-12T00:00:00Z',
  };
}

function provider(persona: Persona) {
  return {
    id: `provider-${persona}`,
    name: `Persona ${persona.toUpperCase()} provider sentinel`,
    provider_type: 'openai_compatible',
    base_url: `https://persona-${persona}.example/v1`,
    enabled: true,
    status: 'ready',
    last_discovery_at: '2026-08-12T00:00:00Z',
    last_error: null,
    discovered_models: [],
    secret_hint: '****safe',
    owner_id: currentUser(persona).id,
    created_at: '2026-08-12T00:00:00Z',
    updated_at: '2026-08-12T00:00:00Z',
  };
}

async function fulfillJson(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

async function installApiHarness(page: Page, options: ApiHarnessOptions = {}) {
  const requests: RequestRecord[] = [];
  const logoutSeen = deferred<RequestRecord>();
  const releaseLogout = deferred();
  const providerCreateSeen = deferred<RequestRecord>();
  const releaseProviderCreate = deferred();
  const providerCreateDone = deferred();

  await page.route('**/api/v1/**', async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const authorization = request.headers()['authorization'] ?? null;
    const record: RequestRecord = {
      authorization,
      body: request.postDataJSON() as unknown,
      method: request.method(),
      path,
    };
    requests.push(record);

    if (path.endsWith('/auth/refresh')) {
      await fulfillJson(route, { title: 'Unauthorized', status: 401 }, 401);
      return;
    }

    if (path.endsWith('/auth/login')) {
      const email = (record.body as { email: string }).email;
      const persona: Persona = email.startsWith('persona-a') ? 'a' : 'b';
      await fulfillJson(route, {
        access_token: `jwt-persona-${persona}`,
        token_type: 'bearer',
        expires_in: 900,
      });
      return;
    }

    if (path.endsWith('/auth/logout')) {
      logoutSeen.resolve(record);
      if (options.delayLogout) await releaseLogout.promise;
      await route.fulfill({ status: 204 });
      return;
    }

    const persona = personaFromBearer(authorization);
    if (path.endsWith('/auth/me') && persona) {
      await fulfillJson(route, currentUser(persona));
      return;
    }

    if (path.endsWith('/admin/members')) {
      await fulfillJson(route, { items: [], next_cursor: null });
      return;
    }

    if (path.endsWith('/admin/groups')) {
      if (persona === 'a' && options.failGroupsForPersonaA) {
        await fulfillJson(route, { title: 'Unauthorized', status: 401 }, 401);
      } else if (persona) {
        await fulfillJson(route, { items: [] });
      } else {
        await fulfillJson(route, { title: 'Unauthorized', status: 401 }, 401);
      }
      return;
    }

    if (path.endsWith('/admin/llm-providers') && request.method() === 'POST') {
      providerCreateSeen.resolve(record);
      if (options.delayProviderCreate) await releaseProviderCreate.promise;
      try {
        await fulfillJson(route, provider('a'), 201);
      } catch {
        // Expected when principal teardown aborts the in-flight browser fetch.
      } finally {
        providerCreateDone.resolve();
      }
      return;
    }

    if (path.endsWith('/admin/llm-providers') && persona) {
      await fulfillJson(route, { items: [provider(persona)] });
      return;
    }

    if (path.endsWith('/mcp-servers') && request.method() === 'GET' && persona) {
      await fulfillJson(route, { items: [], next_cursor: null });
      return;
    }

    await fulfillJson(route, { title: 'Not found', status: 404 }, 404);
  });

  return {
    requests,
    logoutSeen: logoutSeen.promise,
    releaseLogout: () => releaseLogout.resolve(),
    providerCreateSeen: providerCreateSeen.promise,
    releaseProviderCreate: () => releaseProviderCreate.resolve(),
    providerCreateDone: providerCreateDone.promise,
  };
}

async function loginAs(page: Page, persona: Persona) {
  await page.getByLabel(/email/i).fill(`persona-${persona}@example.test`);
  await page.getByLabel(/password/i).fill(`persona-${persona}-password`);
  await page.getByRole('button', { name: /sign in/i }).click();
  await expect(page.getByRole('button', { name: /account menu/i })).toBeVisible();
}

async function openProviders(page: Page) {
  await page.getByRole('tab', { name: 'LLM providers' }).click();
  return page.getByRole('form', { name: /add llm provider/i });
}

interface CookieServerState {
  active: Array<{ persona: Persona; slot: string }>;
  heldLogout: boolean;
  heldLogins: number;
  heldRefreshes: number;
  maxSetCookieHeaders: number;
  requests: Array<{
    cookieHeaderBytes: number;
    method: string;
    path: string;
    slot: string | null;
    status: number | null;
  }>;
}

async function cookieServerState(control: string): Promise<CookieServerState> {
  return (await (await fetch(`${control}/state`)).json()) as CookieServerState;
}

test('external B selection aborts a pre-token A login and restart keeps B (R3-002)', async ({
  page,
}) => {
  test.skip(
    Boolean(process.env.E2E_BASE_URL),
    'The faithful cookie fixture is owned by the default Playwright web servers.',
  );
  const control = 'http://127.0.0.1:4174/__control__';
  await fetch(`${control}/reset`, { method: 'POST' });
  await page.goto('/admin');
  const secondTab = await page.context().newPage();
  await secondTab.goto('/admin');

  await fetch(`${control}/hold-login`, { method: 'POST' });
  await page.getByLabel(/email/i).fill('persona-a@example.test');
  await page.getByLabel(/password/i).fill('persona-a-password');
  await page.getByRole('button', { name: /sign in/i }).click();
  await expect.poll(async () => (await cookieServerState(control)).heldLogins).toBe(1);

  await secondTab.getByLabel(/email/i).fill('persona-b@example.test');
  await secondTab.getByLabel(/password/i).fill('persona-b-password');
  await secondTab.getByRole('button', { name: /sign in/i }).click();
  await expect(secondTab.getByRole('button', { name: /account menu/i })).toBeVisible();
  await fetch(`${control}/release-login`, { method: 'POST' });
  await expect(page.getByRole('button', { name: /account menu/i })).toBeVisible();

  await page.getByRole('button', { name: /account menu/i }).click();
  await expect(page.getByText('persona-b@example.test', { exact: true }).first()).toBeVisible();
  await page.keyboard.press('Escape');
  await page.reload();
  await expect(page.getByRole('button', { name: /account menu/i })).toBeVisible();
  await page.getByRole('button', { name: /account menu/i }).click();
  await expect(page.getByText('persona-b@example.test', { exact: true }).first()).toBeVisible();

  const state = await cookieServerState(control);
  expect(state.active.some(({ persona }) => persona === 'b')).toBe(true);
  // The accepted A response is intentionally released only after B selected.
  // Its unique family may remain as an unselected, valid credential when the
  // browser accepted Set-Cookie before AbortController won; it cannot become
  // authoritative and the server cap/TTL bound it (spec 0004 §2.3).
  if (state.active.some(({ persona }) => persona === 'a')) {
    expect(
      (await page.context().cookies()).some((cookie) =>
        state.active.some(
          ({ persona, slot }) => persona === 'a' && cookie.name === `lumen_refresh_token_${slot}`,
        ),
      ),
    ).toBe(false);
  }
  await secondTab.close();
});

test('200 ambiguous slot logins stay below browser/header budgets and preserve the selected cookie (R3-003/R3-004)', async ({
  page,
}) => {
  test.setTimeout(60_000);
  test.skip(
    Boolean(process.env.E2E_BASE_URL),
    'The faithful cookie fixture is owned by the default Playwright web servers.',
  );
  const control = 'http://127.0.0.1:4174/__control__';
  await fetch(`${control}/reset`, { method: 'POST' });
  await page.goto('/admin');
  await loginAs(page, 'a');

  const selectedCookie = (await page.context().cookies()).find((cookie) =>
    cookie.name.startsWith('lumen_refresh_token_'),
  );
  const selectedSlot = selectedCookie?.name.replace('lumen_refresh_token_', '');
  if (!selectedSlot) throw new Error('Selected cookie missing before bounded-login proof');

  // A wire-equivalent spelling is deliberately not normalized into a second
  // cookie namespace. Chromium must receive the safe 422 with no new cookie.
  const malformedStatus = await page.evaluate(async (slot) => {
    const response = await fetch('/api/v1/auth/login', {
      method: 'POST',
      credentials: 'include',
      headers: {
        'Content-Type': 'application/json',
        'X-Lumen-Auth-Slot': slot.toUpperCase(),
      },
      body: JSON.stringify({
        email: 'persona-a@example.test',
        password: 'persona-a-password',
      }),
    });
    await response.arrayBuffer();
    return response.status;
  }, selectedSlot);
  expect(malformedStatus).toBe(422);

  // Ignore each successful JSON bearer, modelling a response whose HttpOnly
  // cookie was accepted but whose client-side intent never became selected.
  // The original selected slot is sent as non-secret preservation metadata.
  const statuses = await page.evaluate(async (previousSlot) => {
    const results: number[] = [];
    for (let index = 0; index < 200; index += 1) {
      const slot = crypto.randomUUID();
      const response = await fetch('/api/v1/auth/login', {
        method: 'POST',
        credentials: 'include',
        headers: {
          'Content-Type': 'application/json',
          'X-Lumen-Auth-Slot': slot,
          'X-Lumen-Previous-Auth-Slot': previousSlot,
        },
        body: JSON.stringify({
          email: 'persona-a@example.test',
          password: 'persona-a-password',
        }),
      });
      await response.arrayBuffer();
      results.push(response.status);
    }
    return results;
  }, selectedSlot);
  expect(statuses).toHaveLength(200);
  expect(new Set(statuses)).toEqual(new Set([200]));

  const slotCookies = (await page.context().cookies()).filter((cookie) =>
    cookie.name.startsWith('lumen_refresh_token_'),
  );
  expect(slotCookies.length).toBeLessThanOrEqual(8);
  expect(slotCookies.some((cookie) => cookie.name === selectedCookie?.name)).toBe(true);
  expect(slotCookies.every((cookie) => cookie.httpOnly && cookie.sameSite === 'Strict')).toBe(true);

  const state = await cookieServerState(control);
  const loginRequests = state.requests.filter(({ path }) => path.endsWith('/auth/login'));
  expect(Math.max(...loginRequests.map(({ cookieHeaderBytes }) => cookieHeaderBytes))).toBeLessThan(
    4_096,
  );
  expect(state.maxSetCookieHeaders).toBeLessThanOrEqual(2);
  expect(state.active).toHaveLength(slotCookies.length);
  expect(new Set(state.active.map(({ slot }) => slot))).toEqual(
    new Set(slotCookies.map(({ name }) => name.replace('lumen_refresh_token_', ''))),
  );

  // The protected selected family still rotates, even after its Cookie header
  // is reordered behind seven unrelated names and the document restarts.
  const selectedRefresh = await page.evaluate(async (slot) => {
    const response = await fetch('/api/v1/auth/refresh', {
      method: 'POST',
      credentials: 'include',
      headers: { 'X-Lumen-Auth-Slot': slot },
    });
    await response.arrayBuffer();
    return response.status;
  }, selectedSlot);
  expect(selectedRefresh).toBe(200);

  const reordered = (await page.context().cookies()).filter((cookie) =>
    cookie.name.startsWith('lumen_refresh_token_'),
  );
  await page.context().clearCookies();
  await page.context().addCookies([...reordered].reverse());
  await page.reload();
  await expect(page.getByRole('button', { name: /account menu/i })).toBeVisible();
  expect(await page.evaluate(() => localStorage.getItem('lumen.active-auth-slot'))).toBe(
    selectedSlot,
  );
});

for (const responseOrder of ['loser-first', 'winner-first'] as const) {
  test(`two tabs recover a same-slot refresh race without Web Locks (${responseOrder}) (R3-001)`, async ({
    page,
  }) => {
    test.skip(
      Boolean(process.env.E2E_BASE_URL),
      'The faithful cookie fixture is owned by the default Playwright web servers.',
    );
    const control = 'http://127.0.0.1:4174/__control__';
    await fetch(`${control}/reset`, { method: 'POST' });
    await page.goto('/admin');
    await loginAs(page, 'a');
    const secondTab = await page.context().newPage();
    await secondTab.goto('/admin');
    await expect(secondTab.getByRole('button', { name: /account menu/i })).toBeVisible();

    const before = await cookieServerState(control);
    await page.context().addInitScript(() => {
      Object.defineProperty(navigator, 'locks', { configurable: true, value: undefined });
    });
    await fetch(`${control}/hold-refresh`, { method: 'POST' });
    const firstReload = page.reload();
    const secondReload = secondTab.reload();
    await expect.poll(async () => (await cookieServerState(control)).heldRefreshes).toBe(2);
    await fetch(`${control}/release-refresh?order=${responseOrder}`, { method: 'POST' });
    await Promise.all([firstReload, secondReload]);

    await expect(page.getByRole('button', { name: /account menu/i })).toBeVisible();
    await expect(secondTab.getByRole('button', { name: /account menu/i })).toBeVisible();
    expect(await page.evaluate(() => navigator.locks)).toBeUndefined();
    expect(await secondTab.evaluate(() => navigator.locks)).toBeUndefined();

    const after = await cookieServerState(control);
    const raced = after.requests
      .slice(before.requests.length)
      .filter(({ path }) => path.endsWith('/auth/refresh'));
    expect(raced.filter(({ status }) => status === 401)).toHaveLength(1);
    expect(raced.filter(({ status }) => status === 200)).toHaveLength(2);
    expect(after.active).toHaveLength(1);
    expect(
      (await page.context().cookies()).filter((cookie) =>
        cookie.name.startsWith('lumen_refresh_token_'),
      ),
    ).toHaveLength(1);
    await secondTab.close();
  });
}

test('real HttpOnly cookie jar survives held A logout after B login and restart (R2-001/R2-003)', async ({
  page,
}) => {
  test.skip(
    Boolean(process.env.E2E_BASE_URL),
    'The faithful cookie fixture is owned by the default Playwright web servers.',
  );
  const control = 'http://127.0.0.1:4174/__control__';
  await fetch(`${control}/reset`, { method: 'POST' });
  await page.goto('/admin');
  await loginAs(page, 'a');

  const cookieA = (await page.context().cookies()).find((cookie) =>
    cookie.name.startsWith('lumen_refresh_token_'),
  );
  expect(cookieA).toBeDefined();
  expect(cookieA?.httpOnly).toBe(true);
  expect(cookieA?.sameSite).toBe('Strict');
  expect(cookieA?.secure).toBe(false); // faithful local-development policy
  expect(cookieA?.path).toBe('/api/v1/auth');
  const slotA = cookieA?.name.replace('lumen_refresh_token_', '');
  expect(slotA).toMatch(/^[0-9a-f-]{36}$/i);
  if (!slotA) throw new Error('Persona A auth slot cookie was not created');
  expect(await page.evaluate(() => localStorage.getItem('lumen.active-auth-slot'))).toBe(slotA);

  await fetch(`${control}/hold-logout`, { method: 'POST' });
  await page.getByRole('button', { name: /account menu/i }).click();
  await page.getByRole('button', { name: /^sign out$/i }).click();
  await expect(page.getByRole('heading', { name: /sign in to your workspace/i })).toBeVisible();

  // The real A response is still held by the HTTP server. Slot isolation means
  // B need not trust AbortController/header timing and can establish its own jar entry.
  await loginAs(page, 'b');
  const beforeRelease = (await page.context().cookies()).filter((cookie) =>
    cookie.name.startsWith('lumen_refresh_token_'),
  );
  expect(beforeRelease).toHaveLength(2);
  const cookieB = beforeRelease.find((cookie) => cookie.name !== cookieA?.name);
  expect(cookieB?.httpOnly).toBe(true);
  const slotB = cookieB?.name.replace('lumen_refresh_token_', '');
  expect(slotB).toBeTruthy();
  if (!slotB) throw new Error('Persona B auth slot cookie was not created');
  expect(slotB).not.toBe(slotA);
  expect(await page.evaluate(() => localStorage.getItem('lumen.active-auth-slot'))).toBe(slotB);

  await fetch(`${control}/release-logout`, { method: 'POST' });
  await expect
    .poll(async () =>
      (await page.context().cookies())
        .filter((cookie) => cookie.name.startsWith('lumen_refresh_token_'))
        .map((cookie) => cookie.name),
    )
    .toEqual([cookieB?.name]);

  const state = (await (await fetch(`${control}/state`)).json()) as {
    active: Array<{ persona: Persona; slot: string }>;
    heldLogout: boolean;
    requests: Array<{ path: string; slot: string | null }>;
  };
  expect(state.heldLogout).toBe(false);
  expect(state.active).toEqual([{ persona: 'b', slot: slotB }]);
  expect(state.requests.find(({ path }) => path.endsWith('/auth/logout'))?.slot).toBe(slotA);
  expect(state.requests.filter(({ path }) => path.endsWith('/auth/login')).at(-1)?.slot).toBe(
    slotB,
  );

  // Reload proves bootstrap selects B's HttpOnly cookie through the real app
  // coordinator. Selecting the revoked/deleted A slot cannot resurrect A.
  await page.reload();
  await expect(page.getByRole('button', { name: /account menu/i })).toBeVisible();
  await page.getByRole('button', { name: /account menu/i }).click();
  await expect(page.getByText('persona-b@example.test', { exact: true }).first()).toBeVisible();
  await page.keyboard.press('Escape');

  await page.evaluate((staleSlot) => {
    localStorage.setItem('lumen.active-auth-slot', staleSlot);
  }, slotA);
  await page.reload();
  await expect(page.getByRole('heading', { name: /sign in to your workspace/i })).toBeVisible();

  // Re-select B, then prove a second tab clearing/replacing the shared selector
  // logs the old B tab out and revokes/deletes B rather than leaving a session
  // that can resurrect after restart.
  await page.evaluate((activeSlot) => {
    localStorage.setItem('lumen.active-auth-slot', activeSlot);
  }, slotB);
  await page.reload();
  await expect(page.getByRole('button', { name: /account menu/i })).toBeVisible();
  const secondTab = await page.context().newPage();
  await secondTab.goto('/admin');
  await expect(secondTab.getByRole('button', { name: /account menu/i })).toBeVisible();

  await secondTab.evaluate(() => localStorage.removeItem('lumen.active-auth-slot'));
  await expect(page.getByRole('heading', { name: /sign in to your workspace/i })).toBeVisible();
  await expect
    .poll(async () => {
      const latest = (await (await fetch(`${control}/state`)).json()) as {
        active: Array<{ persona: Persona; slot: string }>;
      };
      return latest.active;
    })
    .toEqual([]);
  expect(
    (await page.context().cookies()).filter((cookie) =>
      cookie.name.startsWith('lumen_refresh_token_'),
    ),
  ).toEqual([]);
  await secondTab.reload();
  await expect(
    secondTab.getByRole('heading', { name: /sign in to your workspace/i }),
  ).toBeVisible();
  await secondTab.close();
});

test('logout intent hard-blanks manager values before delayed revocation and late completion (R1-002/R1-005)', async ({
  page,
}) => {
  const api = await installApiHarness(page, {
    delayLogout: true,
    delayProviderCreate: true,
  });
  await page.goto('/admin');

  const loginEmail = page.getByLabel(/email/i);
  const loginPassword = page.getByLabel(/password/i);
  await expect(loginEmail).toHaveAttribute('name', 'email');
  await expect(loginEmail).toHaveAttribute('autocomplete', 'username');
  await expect(loginPassword).toHaveAttribute('name', 'password');
  await expect(loginPassword).toHaveAttribute('autocomplete', 'current-password');
  await loginAs(page, 'a');

  const form = await openProviders(page);
  const name = form.getByLabel(/^name$/i);
  const baseUrl = form.getByLabel(/base url/i);
  const apiKey = form.getByLabel(/api key/i);
  await expect(name).toHaveAttribute('name', 'llm_provider_display_name');
  await expect(baseUrl).toHaveAttribute('type', 'url');
  await expect(baseUrl).toHaveAttribute('name', 'llm_provider_base_url');
  await expect(apiKey).toHaveAttribute('name', 'llm_provider_api_key');
  await expect(apiKey).toHaveAttribute('autocomplete', 'new-password');

  await name.fill('Persona A active provider');
  await baseUrl.fill('https://active-persona-a.example/v1');
  await apiKey.fill('active-persona-a-secret');
  await form.getByRole('button', { name: /add provider/i }).click();
  const activeA = await api.providerCreateSeen;
  expect(activeA.authorization).toBe('Bearer jwt-persona-a');
  expect(activeA.body).toMatchObject({ api_key: 'active-persona-a-secret' });

  await form.getByRole('button', { name: /show api key/i }).click();
  const nameNode = await name.elementHandle();
  const baseUrlNode = await baseUrl.elementHandle();
  const apiKeyNode = await apiKey.elementHandle();
  if (!nameNode || !baseUrlNode || !apiKeyNode) throw new Error('Provider controls not mounted');
  await nameNode.evaluate((element) => {
    (element as HTMLInputElement).value = 'manager-owned-persona-a-name';
  });
  await baseUrlNode.evaluate((element) => {
    (element as HTMLInputElement).value = 'https://manager-owned-persona-a.example/v1';
  });
  await apiKeyNode.evaluate((element) => {
    (element as HTMLInputElement).value = 'manager-owned-persona-a-secret';
  });

  await page.getByRole('button', { name: /account menu/i }).click();
  await page.getByRole('button', { name: /^sign out$/i }).click();
  const logoutRequest = await api.logoutSeen;

  await expect(page.getByRole('heading', { name: /sign in to your workspace/i })).toBeVisible();
  expect(await nameNode.evaluate((element) => (element as HTMLInputElement).value)).toBe('');
  expect(await baseUrlNode.evaluate((element) => (element as HTMLInputElement).value)).toBe('');
  expect(await apiKeyNode.evaluate((element) => (element as HTMLInputElement).value)).toBe('');
  expect(await apiKeyNode.evaluate((element) => (element as HTMLInputElement).type)).toBe(
    'password',
  );
  expect(logoutRequest.authorization).toBe('Bearer jwt-persona-a');

  await loginAs(page, 'b');
  const formB = await openProviders(page);
  await expect(page.getByText('Persona B provider sentinel')).toBeVisible();
  await expect(page.getByText('Persona A provider sentinel')).toHaveCount(0);
  await expect(formB.getByLabel(/^name$/i)).toHaveValue('');
  await expect(formB.getByLabel(/base url/i)).toHaveValue('');
  await expect(formB.getByLabel(/api key/i)).toHaveValue('');

  api.releaseLogout();
  api.releaseProviderCreate();
  await api.providerCreateDone;
  await expect(page.getByRole('button', { name: /account menu/i })).toBeVisible();
  await expect(page.getByText('Persona B provider sentinel')).toBeVisible();
  expect(
    api.requests
      .filter(({ authorization }) => authorization === 'Bearer jwt-persona-b')
      .some(({ body }) => JSON.stringify(body).includes('active-persona-a-secret')),
  ).toBe(false);

  await page.getByRole('link', { name: 'MCP servers' }).click();
  await page
    .getByRole('button', { name: /register server/i })
    .first()
    .click();
  const mcpName = page.getByLabel(/^name$/i);
  const endpoint = page.getByLabel(/endpoint url/i);
  const secret = page.getByLabel(/^secret/i);
  await expect(mcpName).toHaveAttribute('name', 'mcp_server_display_name');
  await expect(endpoint).toHaveAttribute('type', 'url');
  await expect(endpoint).toHaveAttribute('name', 'mcp_server_endpoint_url');
  await expect(secret).toHaveAttribute('name', 'mcp_server_bearer_token');
  await expect(secret).toHaveAttribute('autocomplete', 'new-password');

  const mcpNameNode = await mcpName.elementHandle();
  const endpointNode = await endpoint.elementHandle();
  const secretNode = await secret.elementHandle();
  if (!mcpNameNode || !endpointNode || !secretNode) throw new Error('MCP controls not mounted');
  await mcpNameNode.evaluate((element) => {
    (element as HTMLInputElement).value = 'manager-owned-mcp-name';
  });
  await endpointNode.evaluate((element) => {
    (element as HTMLInputElement).value = 'https://manager-owned-mcp.example';
  });
  await secretNode.evaluate((element) => {
    (element as HTMLInputElement).value = 'manager-owned-mcp-secret';
  });
  await page.getByRole('button', { name: /cancel/i }).click();
  expect(await mcpNameNode.evaluate((element) => (element as HTMLInputElement).value)).toBe('');
  expect(await endpointNode.evaluate((element) => (element as HTMLInputElement).value)).toBe('');
  expect(await secretNode.evaluate((element) => (element as HTMLInputElement).value)).toBe('');
});

test('failed refresh blocks late A cache/mutation data from B and preserves dispatch bearer (R1-001/R1-005)', async ({
  page,
}) => {
  const api = await installApiHarness(page, {
    delayProviderCreate: true,
    failGroupsForPersonaA: true,
  });
  await page.goto('/admin');
  await loginAs(page, 'a');
  const formA = await openProviders(page);
  await expect(page.getByText('Persona A provider sentinel')).toBeVisible();

  await formA.getByLabel(/^name$/i).fill('Persona A pending provider');
  await formA.getByLabel(/base url/i).fill('https://pending-persona-a.example/v1');
  await formA.getByLabel(/api key/i).fill('pending-persona-a-secret');
  await formA.getByRole('button', { name: /add provider/i }).click();
  const pendingA = await api.providerCreateSeen;
  expect(pendingA.authorization).toBe('Bearer jwt-persona-a');
  expect(pendingA.body).toMatchObject({ api_key: 'pending-persona-a-secret' });

  await page.getByRole('tab', { name: 'Groups' }).click();
  await expect(page.getByRole('heading', { name: /sign in to your workspace/i })).toBeVisible();

  await page.evaluate(() => {
    const state = { sawPersonaAAfterBLoginStarted: false };
    Object.assign(window, { __credentialLifecycleWatch: state });
    const observer = new MutationObserver(() => {
      if (document.body.textContent?.includes('Persona A provider sentinel')) {
        state.sawPersonaAAfterBLoginStarted = true;
      }
    });
    observer.observe(document.body, { childList: true, subtree: true, characterData: true });
  });

  await loginAs(page, 'b');
  await openProviders(page);
  await expect(page.getByText('Persona B provider sentinel')).toBeVisible();

  api.releaseProviderCreate();
  await api.providerCreateDone;
  await expect(page.getByText('Persona A provider sentinel')).toHaveCount(0);
  const sawPersonaA = await page.evaluate(
    () =>
      (
        window as typeof window & {
          __credentialLifecycleWatch: { sawPersonaAAfterBLoginStarted: boolean };
        }
      ).__credentialLifecycleWatch.sawPersonaAAfterBLoginStarted,
  );
  expect(sawPersonaA).toBe(false);

  const providerPosts = api.requests.filter(
    ({ method, path }) => method === 'POST' && path.endsWith('/admin/llm-providers'),
  );
  expect(providerPosts).toHaveLength(1);
  expect(providerPosts[0]?.authorization).toBe('Bearer jwt-persona-a');
  expect(
    api.requests
      .filter(({ authorization }) => authorization === 'Bearer jwt-persona-b')
      .some(({ body }) => JSON.stringify(body).includes('pending-persona-a-secret')),
  ).toBe(false);
  expect(api.requests.filter(({ path }) => path.endsWith('/auth/me')).at(-1)?.authorization).toBe(
    'Bearer jwt-persona-b',
  );
});

test('queued A credential variables are destroyed before B comes online (R1-005)', async ({
  page,
}) => {
  const api = await installApiHarness(page);
  await page.goto('/admin');
  await loginAs(page, 'a');
  const formA = await openProviders(page);

  await page.evaluate(() => window.dispatchEvent(new Event('offline')));
  await formA.getByLabel(/^name$/i).fill('Queued Persona A provider');
  await formA.getByLabel(/base url/i).fill('https://queued-persona-a.example/v1');
  await formA.getByLabel(/api key/i).fill('queued-persona-a-secret');
  await formA.getByRole('button', { name: /add provider/i }).click();
  await expect(formA.getByRole('button', { name: /adding/i })).toBeDisabled();

  await page.getByRole('button', { name: /account menu/i }).click();
  await page.getByRole('button', { name: /^sign out$/i }).click();
  await expect(page.getByRole('heading', { name: /sign in to your workspace/i })).toBeVisible();

  await page.evaluate(() => window.dispatchEvent(new Event('online')));
  await loginAs(page, 'b');
  await openProviders(page);
  await expect(page.getByText('Persona B provider sentinel')).toBeVisible();

  const providerPosts = api.requests.filter(
    ({ method, path }) => method === 'POST' && path.endsWith('/admin/llm-providers'),
  );
  expect(providerPosts).toHaveLength(0);
  expect(
    api.requests.some(({ body }) => JSON.stringify(body).includes('queued-persona-a-secret')),
  ).toBe(false);
  expect(api.requests.filter(({ path }) => path.endsWith('/auth/me')).at(-1)?.authorization).toBe(
    'Bearer jwt-persona-b',
  );
});
