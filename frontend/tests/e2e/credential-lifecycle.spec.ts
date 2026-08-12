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
