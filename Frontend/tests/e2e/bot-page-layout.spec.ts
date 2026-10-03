/**
 * The bot page at 1440×900: the bot's name, figures and actions sit in one
 * row of the account workspace's header, the chart fills the left half down
 * to the window's bottom and stays there while the right half scrolls, and no
 * panel scrolls inside itself. On a phone the header's dropdowns stay inside
 * the window. The API is the owner walk's stubs.
 */
import { expect, test, type Locator, type Page } from '@playwright/test';

import type { ChartHistoryResponse } from '../../src/app/components/broker/v2-panel/lib/broker-v2-panel.types';
import {
  STRATEGY_FIRST_BAR_START_MS,
  STRATEGY_RUN_STOPPED_AT_MS,
  fakeStrategyView,
} from '../../src/app/testing/strategy-view-fixtures';
import { OwnerWalkWorld, PAPER_WORKSPACE, WALKED_BOT, type BotPhase } from './support/owner-walk-world';

const WINDOW = { width: 1440, height: 900 };
const PHONE = { width: 390, height: 844 };

/** Two hours of 1-minute bars under the strategy view fixture's decision candles. */
function tapeHistory(): ChartHistoryResponse {
  const bars = Array.from({ length: 120 }, (_, index) => {
    const startMs = STRATEGY_FIRST_BAR_START_MS + index * 60_000;
    return {
      start_ms: startMs, end_ms: startMs + 60_000, open: '500', high: '501', low: '499', close: '500.5', volume: 100,
      source: 'polygon' as const,
    };
  });
  return {
    as_of_ms: STRATEGY_FIRST_BAR_START_MS + 120 * 60_000,
    bars,
    fill_markers: [],
    from_ms: STRATEGY_FIRST_BAR_START_MS,
    to_ms: STRATEGY_FIRST_BAR_START_MS + 120 * 60_000,
    indicator_bar_budget: 0,
    indicator_bar_budget_satisfied: true,
    indicator_bars: [],
    overlay_notices: [],
    strategy_instance_id: WALKED_BOT,
    symbol: 'SPY',
    timeframe: '1m',
    truncated: false,
  };
}

/** The walk leaves the chart's reads unanswered; this answers the two the tape's lanes are drawn
 * from, and the saved-gates read a loaded strategy view makes. */
async function answerTheChartReads(page: Page): Promise<void> {
  await page.route(/\/api\/strategy-gates\/foo_cross$/, (route) =>
    route.fulfill({ json: { strategy_key: 'foo_cross', gates: [] } }));
  await page.route(/\/bots\/[^/]+\/strategy-view$/, (route) => route.fulfill({
    json: fakeStrategyView({ run_stopped_at_ms: STRATEGY_RUN_STOPPED_AT_MS }),
  }));
  await page.route(/\/bots\/[^/]+\/chart\/history(\?|$)/, (route) => route.fulfill({ json: tapeHistory() }));
}

async function openBotPage(
  page: Page,
  phase: BotPhase,
  answer: (page: Page) => Promise<void> = async () => undefined,
): Promise<OwnerWalkWorld> {
  const world = new OwnerWalkWorld();
  world.phase = phase;
  await world.install(page);
  // Registered after the walk's own routes, so these win.
  await answer(page);
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

test('the finished bot’s tape carries the run’s lanes under its 1-minute bars, inside the chart’s cell (#2808)', async ({ page }) => {
  await page.setViewportSize(WINDOW);
  const world = await openBotPage(page, 'finished', answerTheChartReads);
  const chart = page.locator('.bot-board__chart');

  await page.getByRole('tab', { name: 'Tape · 1m' }).click();

  const lanes = page.getByRole('group', { name: 'The run’s events on the chart’s clock' });
  await expect(lanes.getByRole('list')).toHaveText(['Decisions', 'Orders', 'Market data', 'Bot']);
  // The run's start and end are on the tape's own bars, in time order, inside the plot.
  const started = lanes.getByRole('listitem', { name: /^Started / });
  const ended = lanes.getByRole('listitem', { name: /^Ended / });
  await expect(started).toBeInViewport();
  await expect(ended).toBeInViewport();
  const [startedBox, endedBox, chartBox] = await Promise.all([
    started.boundingBox(), ended.boundingBox(), chart.boundingBox(),
  ]);
  expect(startedBox?.x ?? 0).toBeGreaterThan(chartBox?.x ?? 0);
  expect(endedBox?.x ?? 0).toBeGreaterThan(startedBox?.x ?? 0);
  // Every decision bar's close is a mark on the tape.
  await expect(lanes.getByRole('list', { name: 'Decisions' }).locator('.chart-lanes__mark:not(.chart-lanes__mark--off)'))
    .not.toHaveCount(0);

  // The lanes took their rows from the canvas: the cell still ends at the window's bottom and nothing scrolls.
  expect(await bottomOf(lanes)).toBeLessThanOrEqual(await bottomOf(chart));
  expect(await bottomOf(chart)).toBeGreaterThan(WINDOW.height - 24);
  const documentOverflow = await page.evaluate(() =>
    (document.scrollingElement ?? document.documentElement).scrollHeight - window.innerHeight);
  expect(documentOverflow).toBeLessThanOrEqual(0);
  expect(world.unexpected()).toEqual([]);
});

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
