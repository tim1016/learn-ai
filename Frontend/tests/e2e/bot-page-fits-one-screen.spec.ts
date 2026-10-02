/**
 * #2794 R4: the bot page fits one screen. At 1440×900 the document does not
 * scroll for a running bot, a finished one, or one that ended holding; long
 * content scrolls inside its own panel. The API is the owner walk's stubs.
 */
import { expect, test, type Page } from '@playwright/test';

import { OwnerWalkWorld, PAPER_WORKSPACE, WALKED_BOT, type BotPhase } from './support/owner-walk-world';

/** How far the document reaches past the window's bottom edge. */
async function documentOverflow(page: Page): Promise<number> {
  return page.evaluate(() => {
    const scroller = document.scrollingElement ?? document.documentElement;
    return scroller.scrollHeight - window.innerHeight;
  });
}

for (const phase of ['running', 'finished', 'holding'] satisfies BotPhase[]) {
  test(`the ${phase} bot's page fits 1440×900 without scrolling`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    const world = new OwnerWalkWorld();
    world.phase = phase;
    await world.install(page);

    await page.goto(`${PAPER_WORKSPACE}/bots/${WALKED_BOT}`);
    await expect(page.getByRole('toolbar', { name: 'Actions for this bot' })).toBeVisible();
    await expect(page.locator('.bot-board')).toBeVisible();

    // The board sizes itself once the panels above it have loaded.
    await expect.poll(() => documentOverflow(page)).toBeLessThanOrEqual(0);
    for (const panel of ['chart', 'money', 'decisions', 'orders', 'health', 'setup']) {
      await expect(page.locator(`.bot-board__${panel}`)).toBeInViewport({ ratio: 1 });
    }
    expect(world.unexpected()).toEqual([]);
  });
}
