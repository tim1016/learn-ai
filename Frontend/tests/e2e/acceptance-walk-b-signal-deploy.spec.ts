import { expect, test } from '@playwright/test';

import { accountHandlers, deployReadHandlers, PAPER_WORKSPACE_PATH } from './support/acceptance/account-world';
import { lakeHandlers, WALK_NOW_MS } from './support/acceptance/lake-world';
import { MockBackend } from './support/acceptance/mock-backend';
import {
  DEFAULT_SETTINGS,
  DEFAULT_VERSION_ID,
  EARLIER_VERSION_ID,
  EMA_KEY,
  EMA_LABEL,
  STALE_VERSION_ID,
  emaDeployViewHandlers,
  signalStrategyHandlers,
} from './support/acceptance/signal-strategies-world';
import { WalkRecorder } from './support/acceptance/walk-recorder';

/**
 * Acceptance walk (b), PRD #3490 section 12 and slice 36 (#3526): a registered
 * strategy's Explore, its Golden evidence, "Deploy a bot", the account list, and
 * Deploy opened on the exact version, with the strategy chosen once.
 *
 * Today's path for the same task (the slice 1 baseline, #3491) was 8 page
 * visits and 13 clicks, with the strategy chosen again in Golden Search's
 * History and again in Strategy Validation, a Strategy Lab page with no way on
 * to Golden or Deploy, and a "Use in Deploy" that landed on Deployment
 * Validation with "not applied". Here the strategy is the address, every page
 * after it knows it, and the version travels as its own id.
 *
 * Everything a page reads is answered at the network edge, and the walk stops
 * on the Deploy page: it never presses Deploy, never reviews a budget and
 * sends no command, which the ledger at the end proves.
 *
 * Needs the Signal strategies place (bundle 5, PR #3542) in the served build.
 */

test.use({ viewport: { width: 1440, height: 900 } });

const STRATEGY = `/signal-strategies/${EMA_KEY}`;
const ACCOUNT_LIST = '/brokers/alpaca';
const PAPER_DEPLOY = `${PAPER_WORKSPACE_PATH}/deploy`;

test.describe('Acceptance walk (b): a strategy to Deploy, pinned to the exact version', () => {
  test('Explore, Golden, Deploy a bot, the account list and Deploy open on the version Golden offered', async ({ page, baseURL }, info) => {
    test.setTimeout(90_000);
    const api = new MockBackend().use(accountHandlers, emaDeployViewHandlers, deployReadHandlers, lakeHandlers, signalStrategyHandlers);
    await api.install(page, baseURL, WALK_NOW_MS);
    const walk = new WalkRecorder(page, 'b-signal-strategy-deploy');
    const strategySections = page.getByRole('navigation', { name: 'Strategy sections' });

    // 1. The strategy is the address: Explore opens on its Backtest, and the header already names it.
    await walk.open(`${STRATEGY}/explore`);
    walk.choose('strategy', EMA_KEY);
    await expect(page).toHaveURL(new RegExp(`${STRATEGY}/explore/backtest$`));
    await expect(page.getByRole('heading', { level: 1, name: EMA_LABEL })).toBeVisible();
    await expect(strategySections.getByRole('link', { name: 'Explore' })).toHaveAttribute('aria-current', 'page');

    // 2. Golden is the one place settings are approved; it opens on the strategy already named.
    await walk.click(strategySections.getByRole('link', { name: 'Golden', exact: true }));
    await expect(page).toHaveURL(new RegExp(`${STRATEGY}/golden/studies$`));
    await expect(page.getByText('Settings are approved only here')).toBeVisible();
    await expect(page.getByRole('heading', { level: 1, name: EMA_LABEL })).toBeVisible();

    // 3. The qualified versions are the evidence: one symbol and one exact configuration each, with its own status.
    await walk.click(page.getByRole('navigation', { name: 'Golden sections' }).getByRole('link', { name: 'Versions' }));
    await expect(page).toHaveURL(new RegExp(`${STRATEGY}/golden/versions$`));
    await expect(page.getByRole('heading', { level: 2, name: 'Qualified versions' })).toBeVisible();
    const defaultRow = page.getByRole('row', { name: new RegExp(DEFAULT_VERSION_ID) });
    await expect(defaultRow).toContainText('default for SPY');
    await expect(defaultRow).toContainText(`fast_period = ${String(DEFAULT_SETTINGS['fast_period'])}`);
    await expect(page.getByRole('row', { name: new RegExp(EARLIER_VERSION_ID) })).toContainText('Ready');
    await expect(page.getByRole('row', { name: new RegExp(STALE_VERSION_ID) })).toContainText('Stale');
    const versionsOwnLink = await defaultRow.getByRole('link', { name: 'Use in Deploy' }).getAttribute('href');

    // 4. "Deploy a bot" offers what Golden holds, ready versions only; a stale one is listed with why and has no link.
    await walk.click(page.getByRole('button', { name: 'Deploy a bot' }));
    const chooser = page.getByRole('region', { name: 'Deploy a bot' });
    await expect(chooser.getByRole('link', { name: `Deploy version ${EARLIER_VERSION_ID} for SPY` })).toBeVisible();
    await expect(chooser.getByText('Not offered: The program changed after this version was approved')).toBeVisible();
    await expect(chooser.getByRole('link', { name: new RegExp(STALE_VERSION_ID) })).toHaveCount(0);
    const defaultLink = chooser.getByRole('link', { name: `Use the current default ${DEFAULT_VERSION_ID} for SPY` });
    // The version row on Golden and the chooser carry the same intent: this strategy and this exact version.
    await expect(defaultLink).toHaveAttribute('href', versionsOwnLink ?? '');

    // 5. The account list carries the version unchanged and picks no account.
    await walk.click(defaultLink);
    await expect(page).toHaveURL(
      `${new URL(page.url()).origin}${ACCOUNT_LIST}?strategy=${EMA_KEY}&golden_qualification=${DEFAULT_VERSION_ID}`,
    );
    const intent = page.getByRole('status', { name: 'Golden configuration' });
    await expect(intent).toContainText(DEFAULT_VERSION_ID);
    await expect(intent).toContainText(EMA_KEY);
    await expect(intent).toContainText('SPY');
    await expect(intent).toContainText(`fast_period = ${String(DEFAULT_SETTINGS['fast_period'])}`);
    await expect(intent).toContainText(`slow_period = ${String(DEFAULT_SETTINGS['slow_period'])}`);
    await expect(intent).toContainText('It is the current default for SPY');
    await expect(intent).toContainText('Nothing is deployed from this page');
    expect(new URL(page.url()).pathname, 'the list names the accounts and waits for the owner to choose').toBe(ACCOUNT_LIST);

    // 6. Choosing the Paper account opens its Deploy page on that version: strategy, symbol and every setting applied.
    await walk.click(page.getByRole('main').getByRole('link', { name: /Paper/ }).first());
    await expect(page).toHaveURL(new RegExp(`${PAPER_DEPLOY}(\\?.*)?$`));
    const applied = page.getByRole('status', { name: 'Golden configuration' });
    await expect(applied).toContainText(`Golden configuration ${DEFAULT_VERSION_ID.slice(0, 8)}`);
    await expect(applied).toContainText("Its strategy, symbol and exact settings are applied");
    await expect(page.getByText('was not applied')).toHaveCount(0);
    await expect(page.getByLabel('Deployment strategy')).toHaveValue(EMA_KEY);
    await expect(page.getByText('Trades SPY shares, the symbol it listens to')).toBeVisible();
    await expect(page.getByLabel('Fast EMA length')).toHaveValue(String(DEFAULT_SETTINGS['fast_period']));
    await expect(page.getByLabel('Slow EMA length')).toHaveValue(String(DEFAULT_SETTINGS['slow_period']));
    await expect(page.getByLabel('Crossover gap', { exact: true })).toHaveValue(String(DEFAULT_SETTINGS['gap']));
    await expect(page.getByLabel('RSI lower gate')).toHaveValue(String(DEFAULT_SETTINGS['rsi_min']));
    await expect(page.getByLabel('RSI upper gate')).toHaveValue(String(DEFAULT_SETTINGS['rsi_max']));
    await expect(page.getByLabel('Hold time')).toHaveValue(String(DEFAULT_SETTINGS['hold_bars']));
    // It is ready to be filled in, and it is not sent: the walk stops with Deploy still unpressed and refusing for want of a budget.
    await expect(page.getByRole('button', { name: 'Deploy paper bot' })).toBeDisabled();
    walk.stopsAt('Deploy a bot · Paper, prefilled with the version; Deploy paper bot not pressed');

    const counts = await walk.report(info);
    // Today's path is 8 page visits and 13 clicks; this one is the same task in 5 and 5.
    expect(counts.pageVisits, 'page visits').toBeLessThanOrEqual(5);
    expect(counts.clicks, 'clicks').toBeLessThanOrEqual(5);
    expect(counts.typed, 'typed entries').toBe(0);
    expect(counts.reselects, 'a strategy chosen again').toBe(0);
    expect(counts.applyRefreshPresses, 'Apply or Refresh presses').toBe(0);

    // Deploy read exactly the version that was carried, never another in its place.
    const offersRead = api.requests.filter((entry) => entry.includes('/deploy-offer'));
    expect(offersRead.length).toBeGreaterThan(0);
    expect(offersRead.every((entry) => entry.includes(`/golden-qualifications/${DEFAULT_VERSION_ID}/deploy-offer`))).toBe(true);
    // Reads only: no Deploy, no review, no account change, no job.
    expect(api.commands(), 'commands sent').toEqual([]);
    expect(api.unanswered, 'reads nothing answered').toEqual([]);
  });
  test('a version that went stale is refused where it is carried, and no other takes its place', async ({ page, baseURL }) => {
    test.setTimeout(90_000);
    const api = new MockBackend().use(accountHandlers, emaDeployViewHandlers, deployReadHandlers, lakeHandlers, signalStrategyHandlers);
    await api.install(page, baseURL, WALK_NOW_MS);

    // The link as a bookmark, or an owner who waited, would carry it: the strategy and a version that is no longer ready.
    await page.goto(`${ACCOUNT_LIST}?strategy=${EMA_KEY}&golden_qualification=${STALE_VERSION_ID}`);
    await expect(page.getByText('is stale and cannot be deployed')).toBeVisible();
    await expect(page.getByText('No other version was chosen in its place and no account was chosen')).toBeVisible();
    await expect(page.getByRole('status', { name: 'Golden configuration' }), 'nothing is shown as carried').toHaveCount(0);
    await expect(page.getByRole('link', { name: 'Choose a version in Golden' })).toHaveAttribute('href', `${STRATEGY}/golden/versions`);

    // Deploy refuses it by the same facts: it says so, applies nothing, and the version is not swapped for another.
    await page.getByRole('main').getByRole('link', { name: /Paper/ }).first().click();
    await expect(page).toHaveURL(new RegExp(`${PAPER_DEPLOY}(\\?.*)?$`));
    await expect(page.getByText(`Golden configuration ${STALE_VERSION_ID.slice(0, 8)} was not applied.`)).toBeVisible();
    await expect(page.getByText('The program changed after this version was approved')).toBeVisible();
    // The address still names the strategy, so Deploy opens on it, but with its own registered settings: none of the stale version's.
    await expect(page.getByLabel('Deployment strategy')).toHaveValue(EMA_KEY);
    await expect(page.getByLabel('Fast EMA length')).not.toHaveValue(String(DEFAULT_SETTINGS['fast_period']));

    const offersRead = api.requests.filter((entry) => entry.includes('/deploy-offer'));
    expect(offersRead.length).toBeGreaterThan(0);
    expect(offersRead.every((entry) => entry.includes(`/golden-qualifications/${STALE_VERSION_ID}/deploy-offer`))).toBe(true);
    expect(api.commands(), 'commands sent').toEqual([]);
    expect(api.unanswered, 'reads nothing answered').toEqual([]);
  });
});
