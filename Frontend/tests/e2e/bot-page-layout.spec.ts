/**
 * The bot page at 1440×900: the bot's name, figures and actions sit in one
 * row of the account workspace's header, the chart fills the left half down
 * to the window's bottom and stays there while the page scrolls under the
 * header, and no panel scrolls inside itself. The API is the owner walk's
 * stubs.
 */
import { expect, test } from '@playwright/test';

import { OwnerWalkWorld, PAPER_WORKSPACE, WALKED_BOT, type BotPhase } from './support/owner-walk-world';

const WINDOW = { width: 1440, height: 900 };

for (const phase of ['running', 'finished', 'holding'] satisfies BotPhase[]) {
  test(`the ${phase} bot's page keeps its chart in view and scrolls no panel inside itself`, async ({ page }) => {
    await page.setViewportSize(WINDOW);
    const world = new OwnerWalkWorld();
    world.phase = phase;
    await world.install(page);

    await page.goto(`${PAPER_WORKSPACE}/bots/${WALKED_BOT}`);
    const header = page.locator('.account-workspace__header');
    await expect(header.getByRole('toolbar', { name: 'Actions for this bot' })).toBeVisible();
    await expect(page.locator('.bot-board')).toBeVisible();

    // One row: the bot's controls fit beside the account's figures and Deploy a bot.
    expect((await header.boundingBox())?.height).toBeLessThan(80);
    // The chart reaches the window's bottom once the board has sized it.
    const chart = page.locator('.bot-board__chart');
    await expect.poll(async () => {
      const box = await chart.boundingBox();
      return box === null ? 0 : box.y + box.height;
    }).toBeGreaterThan(WINDOW.height - 24);
    await expect(chart).toBeInViewport({ ratio: 1 });

    for (const panel of ['money', 'decisions', 'orders', 'health', 'setup']) {
      const hidden = await page.locator(`.bot-board__${panel}`).evaluate((element) => element.scrollHeight - element.clientHeight);
      expect(hidden, `${panel} scrolls inside itself`).toBeLessThanOrEqual(1);
    }

    // The page scrolls inside itself, under the header; the window never does.
    const documentOverflow = await page.evaluate(() =>
      (document.scrollingElement ?? document.documentElement).scrollHeight - window.innerHeight);
    expect(documentOverflow).toBeLessThanOrEqual(0);
    const scrolled = await page.locator('app-bot-panel-shell').evaluate((host) => {
      host.scrollTo({ top: host.scrollHeight });
      return host.scrollTop;
    });
    expect(scrolled, 'the right-hand panels reach past the window').toBeGreaterThan(0);
    await expect(header).toBeInViewport({ ratio: 1 });
    await expect(chart).toBeInViewport({ ratio: 1 });
    expect(world.unexpected()).toEqual([]);
  });
}
