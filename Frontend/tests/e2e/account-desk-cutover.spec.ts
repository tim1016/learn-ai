import { expect, test } from '@playwright/test';

const RETIRED_INTERACTIVE_BROKER_PATHS = [
  '/broker',
  '/broker/accounts',
  '/broker/account-monitor',
  '/broker/reconciliation',
  '/broker/orders',
  '/broker/session-mirror',
  '/broker/offline-replay',
] as const;

test.describe('Interactive Broker navigation retirement', () => {
  for (const path of RETIRED_INTERACTIVE_BROKER_PATHS) {
    test(`redirects ${path} to the Alpaca account desk`, async ({ page }) => {
      await page.goto(path);

      await expect(page).toHaveURL(/\/brokers\/alpaca(?:$|[?#])/);
    });
  }
});
