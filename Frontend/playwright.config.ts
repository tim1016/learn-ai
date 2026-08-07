// Playwright config for retained end-to-end migration coverage. Tests stub
// their API boundaries with ``page.route()`` and require no external services.
// CI builds the Frontend, serves it on localhost:4200, and runs ``npm run e2e``.

import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './tests/e2e',
  fullyParallel: true,
  forbidOnly: !!process.env['CI'],
  retries: process.env['CI'] ? 2 : 0,
  reporter: [['html', { open: 'never' }]],
  use: {
    baseURL: process.env['PLAYWRIGHT_BASE_URL'] ?? 'http://localhost:4200',
    trace: 'on-first-retry',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
  // webServer left unset on purpose: the suite assumes the container
  // is already up (the CI job spins it up before invoking Playwright).
});
