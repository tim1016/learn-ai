import { expect, test } from '@playwright/test';

import { accountHandlers } from './support/acceptance/account-world';
import { lakeHandlers, WALK_NOW_MS } from './support/acceptance/lake-world';
import { MockBackend } from './support/acceptance/mock-backend';
import { OLD_FEATURE_QUESTION, OLD_FEATURE_RUN_ID, researchHandlers } from './support/acceptance/research-world';
import { WalkRecorder } from './support/acceptance/walk-recorder';

/**
 * Acceptance walk (d) (PRD #3490 section 12, slice 36; T17): a study, its saved
 * runs, an old record opened, and the same record after a reload.
 *
 * Slice 1 (#3491) recorded the walk as it was: Research Lab's Experiments tab
 * opened on its own instrument with an empty table until Load, and after Load
 * said the same sentence it said before it, so an empty history and an unloaded
 * one looked alike; its rows had no link, so an old feature result could only be
 * seen again by running the study again. The one history that did open a record
 * took three more page visits through a different group of tabs.
 *
 * Here the study page links to its own saved runs, the History arrives already
 * filtered to that kind and listing them, every row with a saved detail links
 * to it, and the detail has an address of its own: it reads what was saved and
 * never runs the study again, so a reload shows the same record. The mocked
 * backend has no endpoint that starts a study, so a press of Run study, or any
 * rerun, would be refused and fail the ledger.
 */

test.use({ viewport: { width: 1440, height: 900 } });

const OLD_RUN_ADDRESS = `/research/history/feature-study/${OLD_FEATURE_RUN_ID}`;

function backend(): MockBackend {
  return new MockBackend().use(researchHandlers, accountHandlers, lakeHandlers);
}

test.describe('Acceptance walk (d): study -> saved runs -> old record -> reload', () => {
  test('lists the saved feature runs on arrival, opens an old one at its own address, and shows it again after a reload', async ({ page, baseURL }, info) => {
    test.setTimeout(90_000);
    const api = backend();
    await api.install(page, baseURL, WALK_NOW_MS);
    const walk = new WalkRecorder(page, 'd-research-history');

    // 1. The Feature study, by its address; Run study is on the page and is never pressed.
    await walk.open('/research/feature');
    await expect(page.getByRole('heading', { name: 'Feature study' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Run study' })).toBeVisible();

    // 2. Its saved runs: the study page names the link, and the History opens on that kind with the rows already there.
    await walk.click(page.getByRole('link', { name: 'Saved runs' }));
    await expect(page).toHaveURL(/\/research\/history\?type=feature-study$/);
    await expect(page.getByLabel('Kind')).toHaveValue('feature-study');
    const table = page.getByRole('table', { name: 'Saved studies and runs, newest first' });
    await expect(table.getByRole('row')).toHaveCount(3); // the header and the two feature runs
    await expect(table.getByRole('link', { name: OLD_FEATURE_QUESTION })).toBeVisible();
    await expect(table.getByText('Does a trading signal built on macd_signal')).toHaveCount(0);
    await expect(page.getByRole('status').filter({ hasText: 'Page 1 of 1 · 2 records' })).toBeVisible();

    // 3. The old run opens at an address of its own, with what was saved: what it asked, how it ended, its id.
    await walk.click(table.getByRole('link', { name: OLD_FEATURE_QUESTION }));
    walk.choose('record', OLD_FEATURE_RUN_ID);
    await expect(page).toHaveURL(OLD_RUN_ADDRESS);
    const facts = page.getByTestId('stored-facts');
    await expect(page.getByRole('heading', { name: 'Feature study, saved run' })).toBeVisible();
    await expect(page.getByText(OLD_FEATURE_QUESTION, { exact: true })).toBeVisible();
    await expect(facts).toContainText('Completed');
    await expect(facts.locator('code')).toHaveText(OLD_FEATURE_RUN_ID);
    await expect(page.getByText('Opening it does not run the study again.')).toBeVisible();

    // 4. A reload reads the same record from the same address; nothing was rerun to show it.
    await walk.reload();
    await expect(page).toHaveURL(OLD_RUN_ADDRESS);
    await expect(page.getByTestId('stored-facts').locator('code')).toHaveText(OLD_FEATURE_RUN_ID);
    await expect(page.getByTestId('stored-facts')).toContainText('Completed');
    walk.stopsAt('the saved run, read again after a reload; no study was started');

    expect(api.requested(`/study-records/feature-study/${OLD_FEATURE_RUN_ID}`)).toHaveLength(2);
    expect(api.commands(), 'no study was started and nothing was saved').toEqual([]);
    expect(api.unanswered).toEqual([]);

    const counts = await walk.report(info);
    expect(counts.reselects).toBe(0);
    expect(counts.applyRefreshPresses, 'the saved runs need no Load').toBe(0);
    expect(counts.clicks).toBeLessThanOrEqual(2);
    expect(counts.pageVisits).toBeLessThanOrEqual(4);
  });

  test('goes back from a record to the same filtered list, and lists every kind in one table without a filter', async ({ page, baseURL }) => {
    test.setTimeout(90_000);
    const api = backend();
    await api.install(page, baseURL, WALK_NOW_MS);

    // Back from a record returns to the list it was opened from, still filtered, with its rows.
    await page.goto('/research/history?type=feature-study');
    await page.getByRole('link', { name: OLD_FEATURE_QUESTION }).click();
    await expect(page).toHaveURL(OLD_RUN_ADDRESS);
    await page.goBack();
    await expect(page).toHaveURL(/\/research\/history\?type=feature-study$/);
    await expect(page.getByLabel('Kind')).toHaveValue('feature-study');
    await expect(page.getByRole('table', { name: 'Saved studies and runs, newest first' }).getByRole('row')).toHaveCount(3);

    // The record's own link to all saved runs of its kind lands on the same list.
    await page.goto(OLD_RUN_ADDRESS);
    await page.getByRole('link', { name: /All saved Feature study runs/ }).click();
    await expect(page).toHaveURL(/\/research\/history\?type=feature-study$/);

    // The History with no filter indexes every store in one list: each kind named, each row keyed by its own id.
    await page.goto('/research/history');
    const table = page.getByRole('table', { name: 'Saved studies and runs, newest first' });
    await expect(table.getByRole('row')).toHaveCount(6);
    for (const kind of ['Signal study', 'Rule sandbox run', 'Feature study', 'Feature study (earlier)']) {
      await expect(table.getByRole('cell', { name: kind, exact: true }).first()).toBeVisible();
    }
    // A row of the retired store has no detail to open, and says so rather than offering a link that goes nowhere.
    const earlier = table.getByRole('row').filter({ hasText: 'rsi_14 on AAPL (earlier)' });
    await expect(earlier.getByRole('link')).toHaveCount(0);
    await expect(earlier).toContainText('No detail page for this study');

    expect(api.commands()).toEqual([]);
    expect(api.unanswered).toEqual([]);
  });
});
