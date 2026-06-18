import { test, expect } from '@playwright/test';

/**
 * Skeleton smoke: the app boots and the shell + status surface render. The
 * backend may or may not be reachable in CI/dev, so we assert the shell and the
 * status panel (in ANY of its valid states — that's the quality bar: no blank
 * pane, no perpetual spinner), not a specific dependency outcome.
 */
test('app shell and system-status panel render', async ({ page }) => {
  await page.goto('/');

  // Header / shell. `exact` disambiguates the <h1> title from the welcome
  // note's "Beacon — skeleton" <h2>.
  await expect(page.getByRole('heading', { name: 'Beacon', exact: true })).toBeVisible();
  await expect(page.getByText('Backend status')).toBeVisible();

  // The rail welcome note rendered through the markdown pipeline.
  await expect(
    page.getByRole('heading', { name: /Beacon — skeleton/i }),
  ).toBeVisible();

  // The status panel resolves to one of its real states — never stays blank.
  await expect(
    page
      .getByRole('list', { name: /backend dependencies/i })
      .or(page.getByRole('alert'))
      .or(page.getByText(/checking backend readiness/i)),
  ).toBeVisible();

  // The realtime indicator is present (`exact` avoids the welcome note's
  // "…the realtime badge" prose).
  await expect(page.getByText('Realtime', { exact: true })).toBeVisible();
});

/**
 * The developer pages: the floating overlay reveals links to the standalone docs
 * viewer and features catalog, and both render real content.
 */
test('overlay links reach the docs viewer and features catalog', async ({ page }) => {
  await page.goto('/');

  const trigger = page.getByRole('button', { name: /developer pages/i });
  await expect(trigger).toBeVisible();
  await trigger.hover();

  await page.getByRole('link', { name: /documentation/i }).click();
  await expect(page).toHaveURL(/\/docs\//); // index redirects to a default doc
  await expect(page.getByRole('navigation', { name: /documentation/i })).toBeVisible();

  await page.goto('/features');
  await expect(page.getByRole('heading', { name: 'Features built', level: 1 })).toBeVisible();
  await expect(page.getByRole('heading', { name: /LLM model gateway/i })).toBeVisible();
});
