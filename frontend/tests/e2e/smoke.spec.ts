import { test, expect } from '@playwright/test';

/**
 * Auth gate (issue #48, AC-3): the app root is guarded. With no backend
 * reachable in CI/dev, the boot-time silent refresh fails and the app routes to
 * the login screen — not the shell. We assert that grounded behavior: an
 * accessible email/password form, never a blank pane or a perpetual spinner
 * (quality bar). The authenticated shell is exercised in component tests
 * (RouteGuard / LoginScreen) where the contract responses are mocked.
 */
test('unauthenticated root routes to the login screen', async ({ page }) => {
  await page.goto('/');

  await expect(page.getByRole('heading', { name: /sign in to lumen copilot/i })).toBeVisible();
  await expect(page.getByLabel(/email/i)).toBeVisible();
  await expect(page.getByLabel(/password/i)).toBeVisible();
  await expect(page.getByRole('button', { name: /sign in/i })).toBeVisible();
});

/**
 * The developer pages are standalone, UNGUARDED top-level routes (the floating
 * overlay that links to them lives inside the now auth-gated shell, issue #48,
 * so we navigate to the routes directly). Both render real content.
 */
test('the docs viewer and features catalog render', async ({ page }) => {
  await page.goto('/docs');
  await expect(page).toHaveURL(/\/docs\//); // index redirects to a default doc
  await expect(page.getByRole('navigation', { name: /documentation/i })).toBeVisible();

  await page.goto('/features');
  await expect(page.getByRole('heading', { name: 'Features built', level: 1 })).toBeVisible();
  await expect(page.getByRole('heading', { name: /LLM model gateway/i })).toBeVisible();
});
