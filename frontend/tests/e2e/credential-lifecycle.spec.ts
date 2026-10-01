import { expect, test, type Page } from '@playwright/test';

type Persona = 'a' | 'b';

function currentUser(persona: Persona) {
  return {
    id: `00000000-0000-0000-0000-00000000000${persona === 'a' ? '1' : '2'}`,
    tenant_id: `10000000-0000-0000-0000-00000000000${persona === 'a' ? '1' : '2'}`,
    tenant_name: `Persona ${persona.toUpperCase()} tenant`,
    email: `persona-${persona}@example.test`,
    roles: ['admin'],
    created_at: '2026-09-30T00:00:00Z',
  };
}

async function loginAs(page: Page, persona: Persona) {
  await page.getByLabel(/email/i).fill(`persona-${persona}@example.test`);
  await page.getByLabel(/password/i).fill(`persona-${persona}-password`);
  await page.getByRole('button', { name: /sign in/i }).click();
  await expect(page.getByRole('button', { name: /account menu/i })).toBeVisible();
}

for (const mode of ['typed', 'change-only', 'eventless'] as const) {
  test(`A saves a provider, ${mode} draft clears at logout, then B sees blank fields (#580)`, async ({
    page,
    context,
  }) => {
    // Deterministic API responses exercise the production form, auth transport,
    // cache and local cleanup. Cookie/session concurrency belongs to #646/#647.
    const saved = new Map<Persona, Record<string, unknown>>();
    const requests: Array<{
      path: string;
      method: string;
      authorization: string | null;
      body: unknown;
      url: string;
    }> = [];
    await page.route('**/api/v1/**', async (route) => {
      const request = route.request();
      const path = new URL(request.url()).pathname;
      const authorization = request.headers()['authorization'] ?? null;
      const body: unknown = request.postDataJSON();
      requests.push({ path, method: request.method(), authorization, body, url: request.url() });
      const json = (value: unknown, status = 200) =>
        route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) });
      if (path.endsWith('/auth/login')) {
        const persona = (body as { email: string }).email.startsWith('persona-a') ? 'a' : 'b';
        await json({
          access_token: `jwt-persona-${persona}`,
          token_type: 'bearer',
          expires_in: 900,
        });
        return;
      }
      if (path.endsWith('/auth/refresh')) {
        await json({ title: 'Unauthorized', status: 401 }, 401);
        return;
      }
      const persona: Persona | null =
        authorization === 'Bearer jwt-persona-a'
          ? 'a'
          : authorization === 'Bearer jwt-persona-b'
            ? 'b'
            : null;
      if (!persona) {
        await json({ title: 'Unauthorized', status: 401 }, 401);
        return;
      }
      if (path.endsWith('/auth/logout')) {
        await route.fulfill({ status: 204 });
      } else if (path.endsWith('/auth/me')) {
        await json(currentUser(persona));
      } else if (path.endsWith('/admin/llm-providers')) {
        if (request.method() === 'POST') {
          const draft = body as { name: string; base_url: string };
          const provider = {
            id: `provider-${persona}`,
            name: draft.name,
            provider_type: 'openai_compatible',
            base_url: draft.base_url,
            enabled: true,
            status: 'ready',
            last_error: null,
            discovered_models: [],
            secret_hint: '****safe',
            owner_id: currentUser(persona).id,
            created_at: '2026-09-30T00:00:00Z',
            updated_at: '2026-09-30T00:00:00Z',
          };
          saved.set(persona, provider);
          await json(provider, 201);
        } else {
          await json({ items: saved.has(persona) ? [saved.get(persona)] : [] });
        }
      } else if (path.endsWith('/admin/members')) {
        await json({ items: [], next_cursor: null });
      } else if (path.endsWith('/admin/groups')) {
        await json({ items: [] });
      } else {
        await json({ title: 'Not found', status: 404 }, 404);
      }
    });

    await page.goto('/admin');
    const email = page.getByLabel(/email/i);
    const password = page.getByLabel(/password/i);
    await expect(email).toHaveAttribute('type', 'email');
    await expect(email).toHaveAttribute('name', 'email');
    await expect(email).toHaveAttribute('autocomplete', 'username');
    await expect(password).toHaveAttribute('type', 'password');
    await expect(password).toHaveAttribute('name', 'password');
    await expect(password).toHaveAttribute('autocomplete', 'current-password');
    await loginAs(page, 'a');
    await page.getByRole('tab', { name: 'LLM providers' }).click();
    const form = page.getByRole('form', { name: /add llm provider/i });
    const name = form.getByLabel(/^name$/i);
    const baseUrl = form.getByLabel(/base url/i);
    const apiKey = form.getByLabel(/api key/i);
    await expect(name).toHaveAttribute('name', 'llm_provider_display_name');
    await expect(baseUrl).toHaveAttribute('type', 'url');
    await expect(baseUrl).toHaveAttribute('name', 'llm_provider_base_url');
    await expect(baseUrl).toHaveAttribute('inputmode', 'url');
    await expect(baseUrl).toHaveAttribute('autocapitalize', 'none');
    await expect(baseUrl).toHaveAttribute('spellcheck', 'false');
    await expect(apiKey).toHaveAttribute('type', 'password');
    await expect(apiKey).toHaveAttribute('name', 'llm_provider_api_key');
    await expect(apiKey).toHaveAttribute('autocomplete', 'new-password');
    await expect(apiKey).toHaveValue('');

    const secret = 'persona-a-provider-secret-580';
    await name.fill('Persona A saved provider');
    await baseUrl.fill('https://persona-a.example/v1');
    await apiKey.fill(secret);
    await form.getByRole('button', { name: /add provider/i }).click();
    await expect(page.getByText('Persona A saved provider', { exact: true })).toBeVisible();
    await expect(apiKey).toHaveValue('');
    const write = requests.find(
      ({ path, method }) => path.endsWith('/admin/llm-providers') && method === 'POST',
    );
    expect(write?.authorization).toBe('Bearer jwt-persona-a');
    expect(write?.body).toEqual({
      name: 'Persona A saved provider',
      provider_type: 'openai_compatible',
      base_url: 'https://persona-a.example/v1',
      api_key: secret,
    });

    // Direct property writes model a manager that bypasses React's input event.
    // Retained node handles prove cleanup even after the authenticated form leaves.
    const retainedName = await name.elementHandle();
    const retainedUrl = await baseUrl.elementHandle();
    const retainedKey = await apiKey.elementHandle();
    const detachedSecret = `r9-browser-${mode}-detached-secret`;
    const retainedForm = await form.elementHandle();
    if (mode === 'typed') await apiKey.fill(detachedSecret);
    else if (mode === 'change-only') {
      await apiKey.evaluate((node, key) => {
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!;
        setter.call(node, key);
        node.dispatchEvent(new Event('change', { bubbles: true }));
      }, detachedSecret);
      await expect(apiKey).toHaveJSProperty('defaultValue', detachedSecret);
    } else {
      await apiKey.evaluate(
        (node, key) => ((node as HTMLInputElement).value = key),
        detachedSecret,
      );
      await expect(apiKey).toHaveValue(detachedSecret);
      await expect(apiKey).toHaveJSProperty('defaultValue', '');
    }
    // Reproduce R8's foreign attribute and append/remove text copies. The app
    // clears the controls it owns; these external copies are a documented residual.
    const foreignCopies = await form.evaluateHandle((node, key) => {
      node.setAttribute('data-manager-copy', key);
      const text = document.createElement('span');
      text.hidden = true;
      text.textContent = key;
      node.append(text);
      text.remove();
      return { form: node, text };
    }, detachedSecret);
    if (mode === 'change-only') {
      await apiKey.evaluate((node) => {
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!;
        setter.call(node, '');
        node.dispatchEvent(new Event('change', { bubbles: true }));
      });
      await expect(apiKey).toHaveValue('');
      await expect(apiKey).toHaveJSProperty('defaultValue', '');
    }
    await form.getByRole('button', { name: /show api key/i }).click();
    await name.evaluate((node) => {
      (node as HTMLInputElement).value = 'manager-persona-a';
      (node as HTMLInputElement).defaultValue = 'manager-persona-a';
    });
    await baseUrl.evaluate((node) => {
      (node as HTMLInputElement).value = 'https://manager-persona-a.example';
      (node as HTMLInputElement).defaultValue = 'https://manager-persona-a.example';
    });
    if (mode === 'typed')
      await apiKey.evaluate(
        (node) => ((node as HTMLInputElement).value = 'manager-persona-a-secret'),
      );
    await page.getByRole('button', { name: /account menu/i }).click();
    await page.getByRole('button', { name: /sign out/i }).click();
    await expect(page.getByRole('button', { name: /sign in/i })).toBeVisible();
    await expect
      .poll(() => retainedName!.evaluate((node) => (node as HTMLInputElement).value))
      .toBe('');
    expect(await retainedUrl!.evaluate((node) => (node as HTMLInputElement).value)).toBe('');
    expect(await retainedKey!.evaluate((node) => (node as HTMLInputElement).value)).toBe('');
    expect(await retainedKey!.evaluate((node) => (node as HTMLInputElement).type)).toBe('password');
    expect(await retainedForm!.evaluate((node) => node.isConnected)).toBe(false);
    const residual = await foreignCopies.evaluate(({ form: node, text }) => ({
      attribute: node.getAttribute('data-manager-copy'),
      text: text.textContent,
      connected: text.isConnected,
    }));
    expect(residual).toEqual({ attribute: detachedSecret, text: detachedSecret, connected: false });
    const retainedSnapshot = await retainedForm!.evaluate((root) => {
      const representations: string[] = [];
      function visit(node: Node) {
        if (node.nodeValue) representations.push(node.nodeValue);
        if (node instanceof Element) {
          representations.push(...Array.from(node.attributes, (attribute) => attribute.value));
        }
        if (node instanceof HTMLInputElement || node instanceof HTMLTextAreaElement) {
          representations.push(node.value, node.defaultValue);
        }
        for (const child of node.childNodes) visit(child);
      }
      // The foreign form attribute is deliberately excluded. Inspect every
      // app-rendered descendant's attributes/text and native input defaults.
      for (const child of root.childNodes) visit(child);
      return JSON.stringify(representations);
    });
    for (const sentinel of [detachedSecret, 'manager-persona-a-secret', 'manager-persona-a']) {
      expect(retainedSnapshot).not.toContain(sentinel);
    }
    expect(requests.find(({ path }) => path.endsWith('/auth/logout'))?.authorization).toBe(
      'Bearer jwt-persona-a',
    );

    await loginAs(page, 'b');
    await page.getByRole('tab', { name: 'LLM providers' }).click();
    await expect(form).toBeVisible();
    await expect(name).toHaveValue('');
    await expect(baseUrl).toHaveValue('');
    await expect(apiKey).toHaveValue('');
    await expect(apiKey).toHaveAttribute('type', 'password');
    await expect(page.getByText('Persona A saved provider', { exact: true })).toHaveCount(0);
    await expect(page.getByText(/no llm providers registered/i)).toBeVisible();
    expect(
      requests.filter(({ path }) => path.endsWith('/admin/llm-providers')).at(-1)?.authorization,
    ).toBe('Bearer jwt-persona-b');

    const persistent = await page.evaluate(() => {
      const snapshot = (storage: Storage) =>
        Array.from({ length: storage.length }, (_, index) => {
          const key = storage.key(index) ?? '';
          return [key, storage.getItem(key)];
        });
      return JSON.stringify({
        local: snapshot(localStorage),
        session: snapshot(sessionStorage),
        url: location.href,
      });
    });
    for (const sentinel of [secret, 'persona-a-password', 'manager-persona-a-secret']) {
      expect(persistent).not.toContain(sentinel);
      expect(requests.every(({ url }) => !url.includes(sentinel))).toBe(true);
      expect(await page.locator('body').innerText()).not.toContain(sentinel);
    }
    expect(JSON.stringify(saved.get('a'))).not.toContain(secret);
    expect(context.pages()).toHaveLength(1);
  });
}
