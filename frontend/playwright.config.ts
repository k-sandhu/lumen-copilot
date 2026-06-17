import { defineConfig, devices } from '@playwright/test';

// Smoke E2E only for the skeleton: load the app, assert the shell + status render.
// The dev server is started for the test run; in compose, point BASE_URL at the
// published frontend port instead.
const PORT = 5173;
const baseURL = process.env.E2E_BASE_URL ?? `http://localhost:${PORT}`;

export default defineConfig({
  testDir: './tests/e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? 'github' : 'list',
  use: {
    baseURL,
    trace: 'on-first-retry',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  // When E2E_BASE_URL is provided (e.g. compose), reuse it; otherwise spin up dev.
  webServer: process.env.E2E_BASE_URL
    ? undefined
    : {
        command: 'pnpm dev --host 127.0.0.1 --port 5173',
        url: baseURL,
        reuseExistingServer: !process.env.CI,
        timeout: 120_000,
      },
});
