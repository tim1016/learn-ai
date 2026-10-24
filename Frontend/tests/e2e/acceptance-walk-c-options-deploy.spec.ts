import { expect, test } from '@playwright/test';

import { accountHandlers, deployReadHandlers, PAPER_ACCOUNT, PAPER_CLERK } from './support/acceptance/account-world';
import { lakeHandlers, WALK_NOW_MS } from './support/acceptance/lake-world';
import { MockBackend } from './support/acceptance/mock-backend';
import { operationsHandlers } from './support/acceptance/operations-world';
import {
  optionsDeployHandlers,
  optionsSetupsHandlers,
  RUN_ID,
  SETUP_NAME,
  VERSION_ID,
} from './support/acceptance/options-setups-world';
import { WalkRecorder } from './support/acceptance/walk-recorder';

/**
 * Acceptance walk (c) (PRD #3490 section 12, slice 36): an options setup, one
 * of its tested runs, the approved version that run made, and the handoff to an
 * account's Deploy.
 *
 * `/options-setups/<name>/explore` -> History -> open the tested run ->
 * Take to Golden -> the setup's "Deploy an options bot" -> Deploy this version ->
 * the account list, which names the version it carries -> Paper -> Deploy,
 * with that exact version already chosen. The walk stops there: it never
 * presses Review setup and budget or Deploy, and the mocked backend does not
 * accept a command, so nothing can be placed.
 *
 * Slice 1 (#3491) recorded four ways this walk ended short of its goal:
 * an opened run had no address, so a reload lost it; the golden panel gave
 * "Deploy a bot -> Options bot" as plain text; the account's Options bot form
 * began on "Choose a golden setup" with nothing carried from the page the
 * owner came from, and every option stale and disabled; and the tested setup
 * had to be chosen again. This walk asserts each is gone, with the approved
 * version ready, as it is when the owner has approved the run.
 */

test.use({ viewport: { width: 1440, height: 900 } });

test.describe('Acceptance walk (c): options setup -> tested run -> approved version -> account handoff', () => {
  test('carries the setup, the run and the exact approved version to Deploy without choosing any of them again', async ({ page, baseURL }, info) => {
    test.setTimeout(90_000);
    const api = new MockBackend().use(
      ...operationsHandlers('healthy'),
      accountHandlers,
      deployReadHandlers,
      optionsDeployHandlers,
      optionsSetupsHandlers,
      lakeHandlers,
    );
    await api.install(page, baseURL, WALK_NOW_MS);
    const walk = new WalkRecorder(page, 'c-options-deploy');

    // 1. The saved setup, by its address.
    await walk.open(`/options-setups/${encodeURIComponent(SETUP_NAME)}/explore`);
    walk.choose('setup', SETUP_NAME);
    await expect(page.getByRole('heading', { level: 1, name: SETUP_NAME })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'What this backtest tests' })).toBeVisible();

    // 2. The tested run, from the run history.
    await walk.click(page.getByRole('navigation', { name: 'Explore views' }).getByRole('link', { name: 'History', exact: true }));
    await expect(page.getByRole('heading', { name: 'Runs' })).toBeVisible();
    await expect(page.getByRole('cell', { name: /Net profit\s*\$1,234\.56/ })).toBeVisible();
    await walk.click(page.getByRole('button', { name: `Open run ${RUN_ID}` }));
    walk.choose('run', String(RUN_ID));

    // An opened run has an address of its own (it had none), so a reload or a shared link keeps it.
    await expect(page).toHaveURL(new RegExp(`/options-setups/${SETUP_NAME}/explore\\?view=history&run=${RUN_ID}$`));
    await expect(page.getByRole('heading', { name: 'Backtest run' })).toBeVisible();
    await expect(page.getByText('$1,234.56').first()).toBeVisible();
    // A reload, or a link sent to someone else, opens the same run: the address holds everything it needs.
    await page.reload();
    await expect(page).toHaveURL(new RegExp(`/explore\\?view=history&run=${RUN_ID}$`));
    await expect(page.getByRole('heading', { name: 'Backtest run' })).toBeVisible();

    // 3. Golden, with the run that is open already under review.
    await walk.click(page.getByRole('link', { name: 'Take to Golden' }));
    await expect(page).toHaveURL(new RegExp(`/options-setups/${SETUP_NAME}/golden\\?run=${RUN_ID}$`));
    await expect(page.getByRole('heading', { name: `Reviewing run ${RUN_ID}` })).toBeVisible();
    const version = page.locator(`[data-version="${VERSION_ID}"]`);
    await expect(version.getByText('Single-run approval')).toBeVisible();
    await expect(version.getByText('Ready', { exact: true })).toBeVisible();

    // 4. "Deploy an options bot" lists that version with a link, not a sentence about where Deploy is.
    await walk.click(page.getByRole('button', { name: 'Deploy an options bot' }));
    const choice = page.getByRole('region', { name: 'Deploy an options bot' });
    await expect(choice.locator(`[data-version="${VERSION_ID}"]`).getByText(`run ${RUN_ID}`)).toBeVisible();
    await walk.click(choice.getByRole('link', { name: `Deploy version ${VERSION_ID}` }));

    // 5. The account list says which version it carries, and nothing more is chosen but the account.
    await expect(page).toHaveURL(new RegExp(`/brokers/alpaca\\?options_setup=${VERSION_ID}$`));
    const intent = page.getByRole('status', { name: 'Approved options setup' });
    await expect(intent).toContainText(VERSION_ID);
    await expect(intent).toContainText(`Approved from run ${RUN_ID}`);
    await walk.click(page.getByRole('link', { name: /^Paper PAPER/ }));

    // 6. Deploy opens on that exact version: chosen, ready, and enabled. The walk stops before reviewing it.
    await expect(page).toHaveURL(
      new RegExp(`/brokers/alpaca/clerks/${PAPER_CLERK}/accounts/${PAPER_ACCOUNT}/deploy\\?options_setup=${VERSION_ID}$`),
    );
    await expect(page.getByRole('heading', { name: 'Deploy an Options bot' })).toBeVisible();
    const chosen = page.getByLabel('Approved tested version');
    await expect(chosen).toHaveValue(VERSION_ID);
    const option = chosen.locator('option:checked');
    await expect(option).toHaveText(new RegExp(`${SETUP_NAME} .* run ${RUN_ID} .* ready`));
    await expect(option).toBeEnabled();
    await expect(page.getByText('Ready', { exact: true }).first()).toBeVisible();
    walk.stopsAt('Deploy an Options bot, version chosen: Review setup and budget is not pressed');

    const counts = await walk.report(info);

    // Nothing was approved, reviewed, deployed or started, and every read had an answer.
    await expect(page.getByRole('button', { name: 'Review setup and budget' })).toBeVisible();
    expect(api.commands(), 'the walk stops before any command').toEqual([]);
    expect(api.unanswered, 'every read the pages made was answered').toEqual([]);

    // The usability the redesign promised, against the slice 1 baseline (4 page visits plus 4 in-page
    // views, 10 clicks and a form that could not be completed): nothing is chosen twice or applied.
    expect(counts.reselects, 'no setup, run or version is chosen again').toBe(0);
    expect(counts.applyRefreshPresses, 'no Apply or Refresh is pressed').toBe(0);
    expect(counts.typed, 'nothing is typed').toBe(0);
    expect(counts.pageVisits, 'address load, Golden, the account list, Deploy').toBeLessThanOrEqual(4);
    expect(counts.clicks, 'History, Open, Take to Golden, Deploy an options bot, Deploy this version, Paper').toBeLessThanOrEqual(6);
  });
});
