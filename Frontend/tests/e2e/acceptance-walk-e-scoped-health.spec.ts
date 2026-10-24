import { expect, test, type Page } from '@playwright/test';

import { accountHandlers } from './support/acceptance/account-world';
import { lakeHandlers, WALK_NOW_MS } from './support/acceptance/lake-world';
import { MockBackend } from './support/acceptance/mock-backend';
import {
  ARCHIVE_LOST,
  ARCHIVE_PARTIAL,
  ARCHIVE_PENDING,
  ARCHIVE_UNDERLYING,
  operationsHandlers,
  operationsTabHandlers,
  type ArchiveCondition,
} from './support/acceptance/operations-world';
import { optionsSetupsHandlers, RUN_ID, SETUP_NAME } from './support/acceptance/options-setups-world';
import { WalkRecorder } from './support/acceptance/walk-recorder';

/**
 * Acceptance walk (e) (PRD #3490 section 12, slice 36; T20): a scoped data
 * problem, from the page that depends on it to the Operations tab that shows
 * the evidence, and back.
 *
 * An options setup's Explore says, in one line, that the option archive it
 * depends on needs attention, and links to the Operations tab and card that show
 * it. Following the link lands on that underlying's card; Back returns to the
 * same setup with the same run open, with nothing chosen again.
 *
 * Slice 1 (#3491) recorded the walk as it was: the banner's "Inspect archive
 * health" led to the whole Data Lab shell with five cards above its tabs and
 * no way into the pending sessions, and coming back lost the loaded setup, so
 * the owner opened Saved setups and chose it again. This walk asserts the
 * notice names the problem and its tab, the card is in view, and the round
 * trip loses nothing that is in the address. The archive failing alone (T20)
 * leaves the lake and every other Operations tab usable, and an unreadable
 * archive is "Unknown", never healthy.
 *
 * What does not come back: dates typed on Backtest, which the setup's workspace
 * holds in memory and not in its address, so leaving the setup for Operations
 * clears them. Not asserted here: it is an open question for the owner on #3526.
 */

test.use({ viewport: { width: 1440, height: 900 } });

const SETUP_ADDRESS = `/options-setups/${encodeURIComponent(SETUP_NAME)}/explore`;
const OPEN_RUN_ADDRESS = new RegExp(`${SETUP_NAME}/explore\\?view=history&run=${RUN_ID}$`);

function backend(archive: ArchiveCondition): MockBackend {
  return new MockBackend().use(
    ...operationsHandlers(archive),
    operationsTabHandlers,
    accountHandlers,
    optionsSetupsHandlers,
    lakeHandlers,
  );
}

/** One chip of the Operations header's data health, by its source's name. */
function chip(page: Page, source: string) {
  return page.getByRole('list', { name: 'Data health' }).getByRole('listitem').filter({ hasText: source });
}

test.describe('Acceptance walk (e): scoped health notice -> Operations -> back', () => {
  test('names the archive problem on the page that depends on it, shows its card, and returns to the same setup and run', async ({ page, baseURL }, info) => {
    test.setTimeout(90_000);
    const api = backend('needs-attention');
    await api.install(page, baseURL, WALK_NOW_MS);
    const walk = new WalkRecorder(page, 'e-scoped-health');

    // 1. The saved setup, by its address, with one of its runs open: the work the owner has in hand.
    await walk.open(SETUP_ADDRESS);
    walk.choose('setup', SETUP_NAME);
    await walk.click(page.getByRole('navigation', { name: 'Explore views' }).getByRole('link', { name: 'History', exact: true }));
    await walk.click(page.getByRole('button', { name: `Open run ${RUN_ID}` }));
    walk.choose('run', String(RUN_ID));
    await expect(page).toHaveURL(OPEN_RUN_ADDRESS);
    await expect(page.getByRole('heading', { name: 'Backtest run' })).toBeVisible();

    // 2. The page says what it depends on, and how it stands, in one line; the line names the tab that shows it.
    const notice = page.getByRole('status').filter({ hasText: 'option archive' }).first();
    await expect(notice).toContainText('Needs attention.');
    await expect(notice).toContainText(`${ARCHIVE_UNDERLYING} option archive is missing ${ARCHIVE_PENDING} sessions`);
    await walk.click(notice.getByRole('link', { name: 'Open Option archive' }));

    // 3. Operations opens on that underlying's card, which is in view, with the counts the archive reports.
    await expect(page).toHaveURL(new RegExp(`/operations/option-archive#option-archive-${ARCHIVE_UNDERLYING}$`));
    await expect(page.getByRole('heading', { level: 1, name: 'Operations' })).toBeVisible();
    const card = page.locator(`#option-archive-${ARCHIVE_UNDERLYING}`);
    await expect(card).toBeInViewport();
    await expect(card).toContainText('Pending / partial / lost');
    await expect(card).toContainText(`${ARCHIVE_PENDING} / ${ARCHIVE_PARTIAL} / ${ARCHIVE_LOST}`);
    // The header's chips say the same thing the notice did, and that nothing else is wrong.
    await expect(chip(page, 'Option archive')).toContainText('Needs attention');
    await expect(chip(page, 'Lake')).toContainText('OK');
    await expect(chip(page, 'Backups')).toContainText('OK');

    // 4. Back returns to the same setup with the same run open: nothing is chosen again.
    await walk.back();
    await expect(page).toHaveURL(OPEN_RUN_ADDRESS);
    await expect(page.getByRole('heading', { level: 1, name: SETUP_NAME })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Backtest run' })).toBeVisible();
    await expect(page.getByText('$1,234.56').first()).toBeVisible();
    walk.stopsAt('Back on the setup with run 76 open; nothing re-selected');

    const counts = await walk.report(info);

    // The archive failing alone does not take the lake, the backups or the other tabs with it.
    await page.goto('/operations/lake');
    await expect(page.getByRole('heading', { name: 'What a session needs before a run can read it' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Show SPY sessions' })).toBeVisible();
    await expect(page.getByText('Unavailable')).toHaveCount(0);
    await page.goto('/operations/backups');
    await expect(page.getByRole('heading', { name: 'Lake backup' })).toBeVisible();
    await expect(page.getByText('The latest lake backup is current.')).toBeVisible();

    expect(api.commands(), 'nothing is started from a notice or a tab').toEqual([]);
    expect(api.unanswered, 'every read the pages made was answered').toEqual([]);

    // Against the slice 1 baseline (3 page visits, 6 clicks, the loaded setup lost, one re-select).
    expect(counts.reselects, 'the setup and the run are not chosen again').toBe(0);
    expect(counts.applyRefreshPresses).toBe(0);
    expect(counts.typed).toBe(0);
    expect(counts.pageVisits, 'address load, Operations, back').toBeLessThanOrEqual(3);
    expect(counts.clicks, 'History, Open run, the notice link, Back').toBeLessThanOrEqual(4);
  });

  test('an archive that cannot be read is Unknown on the page and in Operations, and the lake stays usable', async ({ page, baseURL }) => {
    test.setTimeout(90_000);
    const api = backend('unreadable');
    await api.install(page, baseURL, WALK_NOW_MS);

    await page.goto(SETUP_ADDRESS);
    const notice = page.getByRole('status').filter({ hasText: 'could not be read' }).first();
    await expect(notice).toContainText('Unknown.');
    await expect(notice).not.toContainText('OK');
    await notice.getByRole('link', { name: 'Open Option archive' }).click();

    // The tab says it cannot read the archive; it does not show a healthy card, and the header does not say OK for the archive or its backups.
    await expect(page.getByText('Archive status is unavailable')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Retry status' })).toBeVisible();
    await expect(chip(page, 'Option archive')).toContainText('Unknown');
    await expect(chip(page, 'Backups')).toContainText('Unknown');
    await expect(chip(page, 'Lake')).toContainText('OK');

    // The lake and the math checks answer on their own.
    await page.getByRole('navigation', { name: 'Operations' }).getByRole('link', { name: 'Lake', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Show SPY sessions' })).toBeVisible();
    await page.getByRole('navigation', { name: 'Operations' }).getByRole('link', { name: 'Math checks', exact: true }).click();
    await expect(page.getByText('EMA 10 against LEAN')).toBeVisible();

    expect(api.commands()).toEqual([]);
    expect(api.unanswered, 'the archive status is answered with its 503; nothing else is left unanswered').toEqual([]);
  });
});
