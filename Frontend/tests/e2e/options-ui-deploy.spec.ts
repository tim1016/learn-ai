import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page } from '@playwright/test';

import type { DeployBotBody, DeploymentBudgetPreview } from '../../src/app/components/broker/v2-panel/lib/broker-v2-panel.service';
import {
  OPTIONS_WARNING_TO_ACCEPT,
  OPTION_REVIEW_BLOCKED,
  OPTION_REVIEW_PRICED,
  OPTION_REVIEW_WITH_BUDGET,
  OPTION_TRADES_SUMMARY,
} from '../../src/app/components/broker/broker-deploy-page/option-deploy.fixtures';
import {
  LONG_CALL_LEGS,
  OPTION_CATALOGUE,
  resolvedPreview,
} from '../../src/app/components/broker/broker-deploy-page/option-structure/option-structure.fixtures';
import { fakeAccountMoney } from '../../src/app/testing/account-money-fixtures';
import { OwnerWalkWorld, PAPER_API, PAPER_WORKSPACE, WALKED_BOT } from './support/owner-walk-world';

/** Browser coverage of #2913's option Deploy. Every API request is intercepted;
 * the existing owner-walk world supplies account readiness and the receipt. */
async function optionWorld(page: Page, blocked = false): Promise<OwnerWalkWorld> {
  const world = new OwnerWalkWorld();
  await world.install(page);
  await page.route('**/api/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === `${PAPER_API}/bots/option-structures`) {
      await route.fulfill({ json: OPTION_CATALOGUE });
    } else if (url.pathname === `${PAPER_API}/bots/traded-asset` && url.searchParams.get('options') === 'true') {
      await route.fulfill({ json: { refusal: null, warning: OPTIONS_WARNING_TO_ACCEPT } });
    } else if (url.pathname === `${PAPER_API}/bots/option-structure-preview`) {
      const body = request.postDataJSON();
      const preview = resolvedPreview();
      await route.fulfill({ json: {
        ...preview, structure: body.structure, legs_asked: body.legs,
        legs: body.legs.length === 1 ? preview.legs.slice(0, 1) : preview.legs,
      } });
    } else if (url.pathname === `${PAPER_API}/bots/budget-preview` && request.postDataJSON().trades) {
      const body = request.postDataJSON() as DeployBotBody;
      const review = blocked ? OPTION_REVIEW_BLOCKED : body.budget ? OPTION_REVIEW_WITH_BUDGET : OPTION_REVIEW_PRICED;
      const answer: DeploymentBudgetPreview = {
        state: blocked ? 'unavailable' : 'ready',
        detail: blocked ? review.blockers?.[0]?.headline ?? '' : 'Money is ready.',
        world: 'real_paper', risk_revision: 1,
        minimum_budget_usd: '824.09', unreserved_usd: '25000.00', shortcuts: [],
        money_after: fakeAccountMoney(),
        budget_usd: body.budget?.amount_usd ?? null,
        review_token: !blocked && body.budget ? 'option-reviewed-money' : null,
        confirmation_text: null,
        trades_summary: OPTION_TRADES_SUMMARY,
        traded_asset_warning: OPTIONS_WARNING_TO_ACCEPT,
        option_review: review,
      };
      await route.fulfill({ json: answer });
    } else {
      await route.fallback();
    }
  });
  return world;
}

async function build(page: Page, structure: 'Long call' | 'Bull call spread'): Promise<void> {
  const what = page.getByRole('region', { name: 'What', exact: true });
  await what.getByRole('button', { name: 'Trades SPY shares, the symbol it listens to' }).click();
  await what.getByRole('radio', { name: 'Options', exact: true }).check();
  await what.getByRole('button', { name: 'Edit structure' }).click();
  await page.getByRole('button', { name: structure, exact: true }).click();
  await page.getByRole('button', { name: 'Use this structure' }).click();
  await expect(what.getByRole('region', { name: structure, exact: true })).toBeVisible();
}

test('Paper options: builds a long call, reviews its money, records acceptance and deploys its exact rules', async ({ page }) => {
  const world = await optionWorld(page);
  await page.goto(`${PAPER_WORKSPACE}/deploy`);
  await build(page, 'Long call');
  const how = page.getByRole('region', { name: 'How', exact: true });
  await how.getByRole('radio', { name: 'Custom structures', exact: true }).check();
  await how.getByLabel('Whole structures per entry').fill('2');
  const money = page.getByRole('region', { name: 'Money', exact: true });
  await expect(money).toContainText(OPTION_REVIEW_PRICED.money_rows?.[0]?.amount_usd ?? '');
  await money.getByLabel('Dollar budget (USD)').fill('1000.00');
  const confirm = page.getByRole('region', { name: 'Confirm', exact: true });
  const deploy = confirm.getByRole('button', { name: 'Deploy paper bot · set aside $1,000.00' });
  await expect(deploy).toBeDisabled();
  const acknowledgement = confirm.getByRole('region', { name: 'Untested traded asset' });
  await acknowledgement.getByRole('checkbox').check();
  await expect(deploy).toBeDisabled();
  await acknowledgement.getByLabel('Your reason').fill('Try one bought call under the accepted option rules.');
  await expect(deploy).toBeEnabled();
  const axe = await new AxeBuilder({ page }).analyze();
  expect(axe.violations).toEqual([]);
  await page.screenshot({ path: 'test-results/options-deploy-ready.png', fullPage: true });
  await deploy.click();
  await expect(page.getByRole('heading', { name: `${WALKED_BOT} is deployed` })).toBeVisible();
  const [{ body }] = world.commandsTo('/bots');
  expect(body).toMatchObject({
    execution_mode: 'paper', symbol: 'SPY', sizing: { preset: 'custom', quantity: 2 },
    trades: {
      kind: 'options', underlying: 'SPY', structure: 'long_call', legs: LONG_CALL_LEGS,
      order_terms: { gap_limit_percent: '8', entry_wait_seconds: 20, cost_limit_percent: '10', expiry_close_lead_minutes: 45 },
    },
    traded_asset_acknowledgement: {
      acknowledgement: 'I_ACCEPT_UNTESTED_TRADED_ASSET_RISK',
      reason: 'Try one bought call under the accepted option rules.',
    },
    budget: { amount_usd: '1000.00', review_token: 'option-reviewed-money' },
  });
  expect(body).not.toHaveProperty('traded_symbol');
  expect(world.commandsTo('/bots')).toHaveLength(1);
  expect(world.unexpected()).toEqual([]);
});

test('A refused structure shows the backend blockers and cannot be deployed', async ({ page }) => {
  const world = await optionWorld(page, true);
  await page.goto(`${PAPER_WORKSPACE}/deploy`);
  await build(page, 'Bull call spread');
  const confirm = page.getByRole('region', { name: 'Confirm', exact: true });
  for (const blocker of OPTION_REVIEW_BLOCKED.blockers ?? []) {
    await expect(confirm).toContainText(blocker.headline);
    if (blocker.fix) await expect(confirm).toContainText(blocker.fix);
  }
  await page.getByRole('region', { name: 'Money', exact: true }).getByLabel('Dollar budget (USD)').fill('1000.00');
  await expect(confirm.getByRole('button', { name: /^Deploy/ })).toBeDisabled();
  expect(world.commandsTo('/bots')).toHaveLength(0);
  expect(world.unexpected()).toEqual([]);
});
