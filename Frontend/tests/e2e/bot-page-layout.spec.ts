/**
 * The bot page at 1440×900: the bot's name, figures and actions sit in one
 * row of the account workspace's header, the chart fills the left half down
 * to the window's bottom and stays there while the right half scrolls, and no
 * panel scrolls inside itself. On a phone the header's dropdowns stay inside
 * the window. The API is the owner walk's stubs.
 */
import { expect, test, type Locator, type Page } from '@playwright/test';

import { OwnerWalkWorld, PAPER_WORKSPACE, WALKED_BOT, type BotPhase } from './support/owner-walk-world';

const WINDOW = { width: 1440, height: 900 };
const PHONE = { width: 390, height: 844 };

async function openBotPage(page: Page, phase: BotPhase): Promise<OwnerWalkWorld> {
  const world = new OwnerWalkWorld();
  world.phase = phase;
  await world.install(page);
  await page.goto(`${PAPER_WORKSPACE}/bots/${WALKED_BOT}`);
  await expect(page.locator('.account-workspace__header').getByRole('toolbar', { name: 'Actions for this bot' })).toBeVisible();
  return world;
}

async function bottomOf(locator: Locator): Promise<number> {
  const box = await locator.boundingBox();
  return box === null ? 0 : box.y + box.height;
}

/** Fully inside the window's width, the way an owner on a phone can read it. */
async function expectInsideWidth(locator: Locator, width: number): Promise<void> {
  const box = await locator.boundingBox();
  expect(box, 'the dropdown renders').not.toBeNull();
  expect(box?.x).toBeGreaterThanOrEqual(0);
  expect((box?.x ?? 0) + (box?.width ?? 0)).toBeLessThanOrEqual(width);
}

for (const phase of ['running', 'finished', 'holding'] satisfies BotPhase[]) {
  test(`the ${phase} bot's chart fills to the window's bottom, scrolled or not, and no panel scrolls inside itself`, async ({ page }) => {
    await page.setViewportSize(WINDOW);
    const world = await openBotPage(page, phase);
    const header = page.locator('.account-workspace__header');
    await expect(page.locator('.bot-board')).toBeVisible();

    // One row: the bot's controls fit beside the account's figures and Deploy a bot.
    expect((await header.boundingBox())?.height).toBeLessThan(80);
    const chart = page.locator('.bot-board__chart');
    await expect.poll(() => bottomOf(chart)).toBeGreaterThan(WINDOW.height - 24);
    await expect(chart).toBeInViewport({ ratio: 1 });

    for (const panel of ['money', 'decisions', 'orders', 'health', 'setup']) {
      const hidden = await page.locator(`.bot-board__${panel}`).evaluate((element) => element.scrollHeight - element.clientHeight);
      expect(hidden, `${panel} scrolls inside itself`).toBeLessThanOrEqual(1);
    }

    // The right half scrolls as one; the window, the header and the chart stay put.
    const scrolled = await page.locator('.bot-board__side').evaluate((side) => {
      side.scrollTo({ top: side.scrollHeight });
      return side.scrollTop;
    });
    expect(scrolled, 'the right-hand panels reach past the window').toBeGreaterThan(0);
    const documentOverflow = await page.evaluate(() =>
      (document.scrollingElement ?? document.documentElement).scrollHeight - window.innerHeight);
    expect(documentOverflow).toBeLessThanOrEqual(0);
    await expect(header).toBeInViewport({ ratio: 1 });
    await expect(chart).toBeInViewport({ ratio: 1 });
    expect(await bottomOf(chart)).toBeGreaterThan(WINDOW.height - 24);
    expect(world.unexpected()).toEqual([]);
  });
}

test('on a phone the run summary, More and All actions stay inside the window', async ({ page }) => {
  await page.setViewportSize(PHONE);
  const world = await openBotPage(page, 'holding');

  await page.locator('.bot-header__status').click();
  await expectInsideWidth(page.getByRole('region', { name: 'Run summary' }), PHONE.width);

  await page.getByRole('button', { name: /^More actions/ }).click();
  await expectInsideWidth(page.getByRole('group', { name: 'More actions for this bot' }), PHONE.width);

  await page.getByRole('button', { name: 'All actions' }).click();
  await expectInsideWidth(page.getByRole('region', { name: 'All actions for this bot' }), PHONE.width);
  expect(world.unexpected()).toEqual([]);
});
