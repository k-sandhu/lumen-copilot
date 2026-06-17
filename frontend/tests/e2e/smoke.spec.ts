import { test, expect } from '@playwright/test';

/**
 * Skeleton smoke: the app boots and the shell + status surface render. The
 * backend may or may not be reachable in CI/dev, so we assert the shell and the
 * status panel (in ANY of its valid states — that's the quality bar: no blank
 * pane, no perpetual spinner), not a specific dependency outcome.
 */
test('app shell and system-status panel render', async ({ page }) => {
  await page.goto('/');

  // Header / shell.
  await expect(page.getByRole('heading', { name: 'Lumen Copilot' })).toBeVisible();
  await expect(page.getByText('Backend status')).toBeVisible();

  // The rail welcome note rendered through the markdown pipeline.
  await expect(
    page.getByRole('heading', { name: /Lumen Copilot — skeleton/i }),
  ).toBeVisible();

  // The status panel resolves to one of its real states — never stays blank.
  await expect(
    page
      .getByRole('list', { name: /backend dependencies/i })
      .or(page.getByRole('alert'))
      .or(page.getByText(/checking backend readiness/i)),
  ).toBeVisible();

  // The realtime indicator is present.
  await expect(page.getByText('Realtime')).toBeVisible();
});
