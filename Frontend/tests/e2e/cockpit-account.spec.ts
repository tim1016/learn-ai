// PRD #617 — cockpit-account.spec.ts
//
// Account row: clean+consistent → collapsed by default; contaminated →
// expanded non-collapsible; unknown identity → expanded with no
// guessed account ID.

import { expect, test, type Page } from '@playwright/test';

import { buildAccountSummary, buildScenarioStatus, buildSummary } from './fixtures/cockpit-fixtures';

const SID = 'dep_val_smoke_001';

async function installRoutes(
  page: Page,
  account: ReturnType<typeof buildAccountSummary>,
) {
  await page.route('**/api/live-instances', (route) =>
    route.fulfill({ json: [buildSummary({ strategyInstanceId: SID })] }),
  );
  await page.route('**/api/live-instances/account-summary', (route) => route.fulfill({ json: account }));
  await page.route(/\/api\/live-instances\/[^/]+\/status$/, (route) =>
    route.fulfill({ json: buildScenarioStatus({ strategyInstanceId: SID }) }),
  );
}

test.describe('cockpit account row', () => {
  test('Clean + consistent: account row collapsed by default, expandable', async ({ page }) => {
    await installRoutes(page, buildAccountSummary({ identity: 'CONSISTENT', contamination: 'clean' }));
    await page.goto(`/broker/instances/${SID}`);
    const row = page.getByTestId('account-summary');
    await expect(row).toHaveClass(/collapsible/);
    await expect(page.getByTestId('account-detail')).toHaveCount(0);
    // Click expands.
    await page.locator('.account-row').click();
    await expect(page.getByTestId('account-detail')).toBeVisible();
  });

  test('Contaminated: row expanded and non-collapsible', async ({ page }) => {
    await installRoutes(page, buildAccountSummary({ contamination: 'contaminated' }));
    await page.goto(`/broker/instances/${SID}`);
    const row = page.getByTestId('account-summary');
    await expect(row).not.toHaveClass(/collapsible/);
    await expect(page.getByTestId('account-detail')).toBeVisible();
    await expect(page.getByTestId('contamination-verdict')).toContainText('CONTAMINATED');
  });

  test('Unknown identity: row expanded with no guessed account_id', async ({ page }) => {
    await installRoutes(page, buildAccountSummary({ identity: 'UNKNOWN', accountId: null }));
    await page.goto(`/broker/instances/${SID}`);
    await expect(page.getByTestId('account-identity')).toContainText('UNKNOWN');
    await expect(page.getByTestId('account-detail')).toBeVisible();
    // No account_id displayed when null.
    await expect(page.locator('.account-row .account-id')).toContainText('—');
  });

  test('policy_blocks_starts surfaces in the detail body', async ({ page }) => {
    await installRoutes(
      page,
      buildAccountSummary({ contamination: 'contaminated', policyBlocksStarts: true }),
    );
    await page.goto(`/broker/instances/${SID}`);
    await expect(page.getByTestId('policy-blocks-starts')).toBeVisible();
  });
});
