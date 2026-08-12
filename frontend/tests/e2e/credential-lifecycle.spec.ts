import { expect, test } from '@playwright/test';

test('provider credential drafts stay blank across an account switch', async ({ page }) => {
  let principal: 'a' | 'b' | null = null;

  await page.route('**/api/v1/**', async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;

    if (path.endsWith('/auth/refresh')) {
      await route.fulfill({
        status: 401,
        contentType: 'application/problem+json',
        body: JSON.stringify({ title: 'Unauthorized', status: 401 }),
      });
      return;
    }

    if (path.endsWith('/auth/login')) {
      const body = request.postDataJSON() as { email: string };
      principal = body.email.startsWith('persona-a') ? 'a' : 'b';
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          access_token: `jwt-persona-${principal}`,
          token_type: 'bearer',
          expires_in: 900,
        }),
      });
      return;
    }

    if (path.endsWith('/auth/logout')) {
      principal = null;
      await route.fulfill({ status: 204 });
      return;
    }

    if (path.endsWith('/auth/me') && principal) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          id: `00000000-0000-0000-0000-00000000000${principal === 'a' ? '1' : '2'}`,
          tenant_id: `10000000-0000-0000-0000-00000000000${principal === 'a' ? '1' : '2'}`,
          email: `persona-${principal}@example.test`,
          roles: ['admin'],
          created_at: '2026-08-11T00:00:00Z',
        }),
      });
      return;
    }

    if (path.endsWith('/admin/llm-providers')) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ items: [] }),
      });
      return;
    }

    await route.fulfill({
      status: 404,
      contentType: 'application/problem+json',
      body: JSON.stringify({ title: 'Not found', status: 404 }),
    });
  });

  await page.goto('/admin');
  const email = page.getByLabel(/email/i);
  const password = page.getByLabel(/password/i);
  await expect(email).toHaveAttribute('name', 'email');
  await expect(email).toHaveAttribute('autocomplete', 'username');
  await expect(password).toHaveAttribute('name', 'password');
  await expect(password).toHaveAttribute('autocomplete', 'current-password');

  await email.fill('persona-a@example.test');
  await password.fill('persona-a-password');
  await page.getByRole('button', { name: /sign in/i }).click();
  await page.getByRole('tab', { name: 'LLM providers' }).click();

  const formA = page.getByRole('form', { name: /add llm provider/i });
  await formA.getByLabel(/^name$/i).fill('Persona A provider');
  await formA.getByLabel(/base url/i).fill('https://persona-a.example/v1');
  await formA.getByLabel(/api key/i).fill('persona-a-provider-secret');

  await page.getByRole('button', { name: /account menu/i }).click();
  await page.getByRole('button', { name: /sign out/i }).click();
  await expect(page.getByRole('heading', { name: /sign in to your workspace/i })).toBeVisible();
  await expect(page.getByLabel(/password/i)).toHaveValue('');

  await page.getByLabel(/email/i).fill('persona-b@example.test');
  await page.getByLabel(/password/i).fill('persona-b-password');
  await page.getByRole('button', { name: /sign in/i }).click();
  await page.getByRole('tab', { name: 'LLM providers' }).click();

  const formB = page.getByRole('form', { name: /add llm provider/i });
  await expect(formB.getByLabel(/^name$/i)).toHaveValue('');
  await expect(formB.getByLabel(/base url/i)).toHaveValue('');
  await expect(formB.getByLabel(/api key/i)).toHaveValue('');

  const browserStorage = await page.evaluate(() =>
    JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage } }),
  );
  expect(browserStorage).not.toContain('persona-a-provider-secret');
  expect(page.url()).not.toContain('persona-a-provider-secret');
});
