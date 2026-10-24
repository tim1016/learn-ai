import { expect, test, type Page } from '@playwright/test';

import { accountHandlers } from './support/acceptance/account-world';
import { WALK_NOW_MS, lakeHandlers } from './support/acceptance/lake-world';
import { MISSING_SESSION, marketsHandlers } from './support/acceptance/markets-world';
import { MockBackend } from './support/acceptance/mock-backend';
import { WalkRecorder } from './support/acceptance/walk-recorder';

/**
 * Acceptance walk (a), PRD #3490 section 12: Markets, an instrument and its
 * window, the same context on Returns and Data, and a dataset ready to build.
 *
 * `/markets` -> SPY -> a window -> Returns -> Data -> Build dataset. The walk
 * stops on Build dataset, before Generate, and sends no command: the only
 * POSTs are reads (the chart, the Returns study, the dataset plan).
 *
 * Slice 1 (#3491) recorded today's version of this task: 6 page visits,
 * 16 clicks, 2 typed entries, 3 re-selects (SPY twice, 1Y once), 2 Apply
 * presses, and two dead ends: the quality evidence that waited for a chart,
 * and a committed scope that a reload or a visit to the Data Lake lost. What
 * this walk holds the redesign to:
 *
 * - The window and the instrument are chosen once. Each tab shows them, the
 *   address names them, and a reload or Back keeps them (nothing is chosen
 *   again, nothing is applied).
 * - Opening the chart loads its quality evidence with it.
 * - Opening Returns or Data fetches nothing: the Returns study runs when
 *   Run is pressed, and the Data tab reports what the lake holds, gap and
 *   all, before anything could be filled.
 *
 * Accepted differences from the baseline, each by design: the Returns study
 * is one explicit Run click (it used to start on its own and fetch what the
 * lake lacked), and the chart loads the default window on arrival, so one
 * chart read answers the window the instrument opened on before the window
 * chosen here.
 */

test.use({ viewport: { width: 1440, height: 900 } });

const FROM = '2026-03-27';
const TO = '2026-09-18';
const SESSIONS = 126;
/** The one session in the window the lake lacks, so Returns studies one fewer than the window schedules. */
const STUDIED = SESSIONS - 1;
const WINDOW_QUERY = `from=${FROM}&to=${TO}`;

const day = (ms: number): string => new Date(ms).toISOString().slice(0, 10);

interface Walked {
  readonly api: MockBackend;
  readonly walk: WalkRecorder;
}

async function begin(page: Page, origin: string | undefined, name: string): Promise<Walked> {
  const api = new MockBackend().use(accountHandlers, lakeHandlers, marketsHandlers);
  await api.install(page, origin, WALK_NOW_MS);
  return { api, walk: new WalkRecorder(page, name) };
}

/** What the context bar shows: the window and how many sessions it holds. */
async function expectWindowShown(page: Page): Promise<void> {
  await expect(page.getByRole('textbox', { name: 'Start date' })).toHaveValue(FROM);
  await expect(page.getByRole('textbox', { name: 'End date' })).toHaveValue(TO);
  await expect(page.getByText(`${SESSIONS} sessions`)).toBeVisible();
}

/** The request bodies the page sent to one POST path, decoded. */
function bodiesTo(api: MockBackend, path: string): unknown[] {
  return api.sent.filter((call) => call.path === path).map((call) => call.body);
}

/** The window a chart or plan request named, as the calendar dates it spans. */
function chartWindows(api: MockBackend): string[] {
  return bodiesTo(api, '/api/chart/data').map((body) => {
    const { from_date, to_date } = body as { from_date: string; to_date: string };
    return `${from_date}..${to_date}`;
  });
}

async function openInstrumentFromTheList(page: Page, walk: WalkRecorder): Promise<void> {
  await walk.open('/markets');
  await expect(page.getByRole('table', { name: 'Held instruments' })).toBeVisible();
  await expect(page.getByRole('row', { name: /SPY/ })).toContainText('$659.28');
  walk.choose('symbol', 'SPY');
  await walk.click(page.getByRole('row', { name: /SPY/ }).getByRole('link'));
}

async function openInstrumentFromSearch(page: Page, walk: WalkRecorder): Promise<void> {
  await walk.open('/markets');
  await expect(page.getByRole('table', { name: 'Held instruments' })).toBeVisible();
  const search = page.getByRole('combobox');
  await walk.click(search);
  await walk.type(search.getByPlaceholder('Search symbol or name…'), 'SPY');
  // The list is the search's answer once it says how many symbols match, not the recents shown before.
  await expect(page.getByText('1 match')).toBeVisible();
  walk.choose('symbol', 'SPY');
  await walk.click(page.getByRole('option', { name: /SPY/ }));
}

/** Everything after the instrument opens: window, Returns, Data, Build dataset. */
async function walkTheInstrument(page: Page, walk: WalkRecorder, api: MockBackend): Promise<void> {
  // Chart: the instrument opens on its default window, and the quality evidence arrives with the chart.
  await expect(page).toHaveURL(/\/markets\/SPY\/chart/);
  await expect(page.getByRole('heading', { name: 'SPY', exact: true })).toBeVisible();
  await expect(page.getByText('Quality clean')).toBeVisible();
  await expect(page.getByRole('button', { name: 'View quality report' })).toBeVisible();
  await expect(page.getByText('No chart loaded')).toHaveCount(0);

  // Window: one choice, carried by the address.
  walk.choose('window', '6M');
  await walk.click(page.getByRole('group', { name: 'Quick ranges' }).getByRole('button', { name: /6M/ }));
  await expect(page).toHaveURL(new RegExp(WINDOW_QUERY));
  await expectWindowShown(page);
  await expect(page.getByText('Quality clean')).toBeVisible();
  await expect.poll(() => chartWindows(api)).toHaveLength(2);
  // One read for the window the instrument opened on, one for the window chosen: nothing is read twice.
  expect(chartWindows(api)[1]).toBe(`${FROM}..${TO}`);

  // Returns: the same window, no new choice, and nothing fetched until Run.
  await walk.click(page.getByRole('navigation', { name: 'Instrument sections' }).getByRole('link', { name: 'Returns' }));
  await expect(page).toHaveURL(new RegExp(`/markets/SPY/returns\\?.*${WINDOW_QUERY}`));
  await expectWindowShown(page);
  await expect(page.getByText('Opening this tab fetches nothing.')).toBeVisible();
  expect(api.requested('/research/return-distribution')).toEqual([]);

  await walk.click(page.getByRole('button', { name: 'Run the study' }));
  await expect(page.locator('dt', { hasText: 'Days studied' }).locator('xpath=following-sibling::dd')).toHaveText(String(STUDIED));
  const studies = bodiesTo(api, '/api/research/return-distribution') as { from_ms_utc: number; to_ms_utc: number; symbol: string }[];
  expect(studies).toHaveLength(1);
  expect({ symbol: studies[0].symbol, from: day(studies[0].from_ms_utc), to: day(studies[0].to_ms_utc) }).toEqual({ symbol: 'SPY', from: FROM, to: TO });
  await expect(page.getByText('1 scheduled session(s) in the window are not captured in the lake')).toBeVisible();

  // Data: the gap is on the page before anything fills it.
  await walk.click(page.getByRole('navigation', { name: 'Instrument sections' }).getByRole('link', { name: 'Data' }));
  await expect(page).toHaveURL(new RegExp(`/markets/SPY/data\\?.*${WINDOW_QUERY}`));
  await expectWindowShown(page);
  await expect(page.getByText(`A run cannot read 1 of the ${SESSIONS} finished sessions in this window.`)).toBeVisible();
  const gaps = page.getByRole('region', { name: 'Gaps in the window' });
  await expect(gaps).toContainText('1 stretch of sessions a run cannot read.');
  await expect(gaps.getByRole('row', { name: new RegExp(MISSING_SESSION) })).toContainText('Missing');
  await expect(page.getByRole('button', { name: 'Fill 1 session' })).toBeEnabled();
  const reads = api.requested('/data-lake/instrument-data');
  expect(reads).toHaveLength(1);
  const asked = new URLSearchParams(reads[0].split('?')[1]);
  expect({ from: day(Number(asked.get('start_trading_date_ms'))), to: day(Number(asked.get('end_trading_date_ms'))) }).toEqual({ from: FROM, to: TO });
  // The provider's bars are fetched only when quality analysis is asked for.
  expect(api.requested('/data-quality/analyze')).toEqual([]);

  // Build dataset: SPY and the window, with the plan Python authored for them. Generate stays unpressed.
  await walk.click(page.getByRole('link', { name: 'Build dataset' }));
  await expect(page).toHaveURL(new RegExp(`/markets/SPY/data/build\\?.*${WINDOW_QUERY}`));
  await expectWindowShown(page);
  await expect(page.getByRole('heading', { name: 'Build dataset' })).toBeVisible();
  await expect(page.locator('dt', { hasText: 'Ticker' }).locator('xpath=following-sibling::dd[1]')).toContainText('SPY');
  await expect(page.locator('dt', { hasText: 'Session count' }).locator('xpath=following-sibling::dd[1]')).toHaveText(String(SESSIONS));
  const plans = bodiesTo(api, '/api/dataset/plan') as { ticker: string; from_date: string; to_date: string }[];
  expect(plans.length).toBeGreaterThan(0);
  expect(plans.every((plan) => plan.ticker === 'SPY' && plan.from_date === FROM && plan.to_date === TO)).toBe(true);
  await expect(page.getByRole('button', { name: 'Generate dataset ZIP' })).toBeVisible();
  walk.stopsAt('Build dataset, before Generate dataset ZIP');
}

/** The scope lives in the address, so a reload or Back lands on the same instrument and window. */
async function expectScopeSurvivesReloadAndBack(page: Page): Promise<void> {
  await page.reload();
  await expect(page).toHaveURL(new RegExp(`/markets/SPY/data/build\\?.*${WINDOW_QUERY}`));
  await expectWindowShown(page);
  await expect(page.getByRole('heading', { name: 'SPY', exact: true })).toBeVisible();
  await expect(page.locator('dt', { hasText: 'Session count' }).locator('xpath=following-sibling::dd[1]')).toHaveText(String(SESSIONS));

  await page.goBack();
  await expect(page).toHaveURL(new RegExp(`/markets/SPY/data\\?.*${WINDOW_QUERY}`));
  await expectWindowShown(page);
}

test.describe('Acceptance walk (a): Markets to a dataset', () => {
  test('opens SPY from the held list and carries one window to Returns, Data and Build dataset', async ({ page, baseURL }, info) => {
    test.setTimeout(90_000);
    const { api, walk } = await begin(page, baseURL, 'a-markets');

    await openInstrumentFromTheList(page, walk);
    await walkTheInstrument(page, walk, api);

    const counts = await walk.report(info);
    // Slice 1 baseline: 6 visits, 16 clicks, 2 typed, 3 re-selects, 2 Apply presses.
    expect(counts.pageVisits).toBeLessThanOrEqual(5);
    expect(counts.clicks).toBeLessThanOrEqual(6);
    expect(counts.typed).toBe(0);
    expect(counts.reselects).toBe(0);
    expect(counts.applyRefreshPresses).toBe(0);

    expect(api.commands(), 'the walk stops before any command').toEqual([]);
    expect(api.unanswered, 'every read the pages made was answered').toEqual([]);

    await expectScopeSurvivesReloadAndBack(page);
  });

  test('finds SPY through search, the path slice 1 walked, with the same counts after it', async ({ page, baseURL }, info) => {
    test.setTimeout(90_000);
    const { api, walk } = await begin(page, baseURL, 'a-markets-search');

    await openInstrumentFromSearch(page, walk);
    await walkTheInstrument(page, walk, api);

    const counts = await walk.report(info);
    // Opening the picker, typing and choosing are the three gestures slice 1 counted; the rest is the same walk.
    expect(counts.pageVisits).toBeLessThanOrEqual(5);
    expect(counts.clicks).toBeLessThanOrEqual(7);
    expect(counts.typed).toBe(1);
    expect(counts.reselects).toBe(0);
    expect(counts.applyRefreshPresses).toBe(0);

    expect(api.commands(), 'the walk stops before any command').toEqual([]);
    expect(api.unanswered, 'every read the pages made was answered').toEqual([]);
  });
});
