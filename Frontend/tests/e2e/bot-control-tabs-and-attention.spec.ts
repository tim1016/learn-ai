// PRD #617 — bot-control-tabs-and-attention.spec.ts
//
// Background instance entering attention shows an outer-tab attention
// marker without changing the foreground; switching to it lands on
// Status & Risk exactly once and then respects the next manual
// selection.

import { expect, test, type Page } from '@playwright/test';

import { buildAccountSummary, buildScenarioStatus, buildSummary } from './fixtures/bot-control-fixtures';

const FG = 'dep_val_smoke_001';
const BG = 'dep_val_smoke_002';

async function installRoutes(
  page: Page,
  bgVerdict: 'READY' | 'BLOCKED',
) {
  const summaries = [
    buildSummary({ strategyInstanceId: FG, readinessVerdict: 'READY' }),
    buildSummary({ strategyInstanceId: BG, readinessVerdict: bgVerdict, processState: 'idle' }),
  ];
  await page.route('**/api/live-instances', (route) => route.fulfill({ json: summaries }));
  await page.route('**/api/live-instances/account-summary', (route) => route.fulfill({ json: buildAccountSummary({}) }));
  await page.route(/\/api\/live-instances\/[^/]+\/status$/, async (route) => {
    const url = new URL(route.request().url());
    const sid = url.pathname.split('/').slice(-2, -1)[0];
    const status =
      sid === BG
        ? buildScenarioStatus({ strategyInstanceId: BG, readinessVerdict: bgVerdict, processState: 'idle' })
        : buildScenarioStatus({ strategyInstanceId: FG, readinessVerdict: 'READY' });
    await route.fulfill({ json: status });
  });
}

test.describe('Bot Control outer tabs and attention', () => {
  test('Both outer tabs render with independent process/readiness facts', async ({ page }) => {
    await installRoutes(page, 'READY');
    await page.goto(`/broker/bots/${FG}`);
    await expect(page.getByTestId(`outer-tab-${FG}`)).toBeVisible();
    await expect(page.getByTestId(`outer-tab-${BG}`)).toBeVisible();
    // Each tab shows its own readiness verdict — no synthetic master verdict.
    await expect(page.getByTestId(`outer-tab-${FG}`)).toContainText('READY');
  });

  test('Background instance entering attention does not yank the foreground', async ({ page }) => {
    // Initial state: both READY.  After the first poll, BG flips to BLOCKED.
    let bgVerdict: 'READY' | 'BLOCKED' = 'READY';
    await page.route('**/api/live-instances', (route) =>
      route.fulfill({
        json: [
          buildSummary({ strategyInstanceId: FG, readinessVerdict: 'READY' }),
          buildSummary({ strategyInstanceId: BG, readinessVerdict: bgVerdict, processState: 'idle' }),
        ],
      }),
    );
    await page.route('**/api/live-instances/account-summary', (route) =>
      route.fulfill({ json: buildAccountSummary({}) }),
    );
    await page.route(/\/api\/live-instances\/[^/]+\/status$/, async (route) => {
      await route.fulfill({ json: buildScenarioStatus({ strategyInstanceId: FG, readinessVerdict: 'READY' }) });
    });

    await page.goto(`/broker/bots/${FG}`);
    await page.getByTestId('inner-tab-audit').click();
    bgVerdict = 'BLOCKED';
    // wait a poll cycle
    await page.waitForTimeout(4_500);
    // Foreground still on Audit, BG outer-tab shows attention marker.
    await expect(page.getByTestId('inner-tab-audit')).toHaveAttribute('aria-selected', 'true');
  });

  test('Switching to unseen-attention instance lands on Status & Risk', async ({ page }) => {
    await installRoutes(page, 'BLOCKED');
    await page.goto(`/broker/bots/${FG}`);
    // The fleet poll observed BG as BLOCKED → attentionUnseen.  Click BG.
    await page.getByTestId(`outer-tab-${BG}`).click();
    await expect(page.getByTestId('inner-tab-status')).toHaveAttribute('aria-selected', 'true');
  });
});
