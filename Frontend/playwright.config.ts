// PRD #607 / Slice 2 (#609) — Playwright config for the cockpit E2E
// suite.  The suite intercepts `/api/live-instances/{id}/status` via
// ``page.route()`` and injects Slice-1 contract-test snapshots; no
// external services required.  The CI job builds the Frontend
// container, starts it on localhost:4200, and runs ``npm run e2e``.

import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './tests/e2e',
  fullyParallel: true,
  forbidOnly: !!process.env['CI'],
  retries: process.env['CI'] ? 2 : 0,
  reporter: [['html', { open: 'never' }]],
  use: {
    baseURL: 'http://localhost:4200',
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
