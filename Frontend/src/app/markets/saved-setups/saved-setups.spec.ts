import type { HttpRequest } from '@angular/common/http';
import type { HttpTestingController } from '@angular/common/http/testing';
import { screen, waitFor, within } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { environment } from '../../../environments/environment';
import { DATA_LAB_ROUTES } from '../../components/data-lab/data-lab.routes';
import {
  CHART_URL,
  chartRequest,
  chartResponse,
  flushCatalog,
  openPlace,
  type OpenedPlace,
} from '../testing/instrument-place';

// The canvas chart is not exercised here — what it was asked for and shown is.
vi.mock('lightweight-charts', () => {
  const series = () => ({ setData: vi.fn(), update: vi.fn(), applyOptions: vi.fn(), createPriceLine: vi.fn() });
  const chart = () => ({
    addSeries: vi.fn(series),
    removeSeries: vi.fn(),
    remove: vi.fn(),
    applyOptions: vi.fn(),
    resize: vi.fn(),
    timeScale: vi.fn(() => ({
      applyOptions: vi.fn(),
      fitContent: vi.fn(),
      getVisibleLogicalRange: vi.fn(() => null),
      setVisibleLogicalRange: vi.fn(),
      setVisibleRange: vi.fn(),
      subscribeVisibleLogicalRangeChange: vi.fn(),
    })),
    subscribeCrosshairMove: vi.fn(),
    priceScale: vi.fn(() => ({ applyOptions: vi.fn() })),
  });
  return {
    createChart: vi.fn(chart),
    CandlestickSeries: 'CandlestickSeries',
    HistogramSeries: 'HistogramSeries',
    LineSeries: 'LineSeries',
    TickMarkType: { Year: 0, Month: 1, DayOfMonth: 2, Time: 3, TimeWithSeconds: 4 },
  };
});

const SESSIONS_URL = `${environment.pythonServiceUrl}/api/data-lab/sessions`;
const SPY_SETUP = '6f9619ff-8b86-d011-b42d-00c04fc964ff';
const QQQ_SETUP = '3f2504e0-4f89-11d3-9a0c-0305e82c3301';
const OPENED_ADDRESS = '/markets/SPY/chart?timeframe=5m&session=extended&from=2026-04-15&to=2026-05-15';
const ELSEWHERE = 'from=2026-01-02&to=2026-02-27';
const OLD_ADDRESSES = [{ path: 'data-lab', loadChildren: () => DATA_LAB_ROUTES }];

/** One row of Python's list: the facts of a saved setup, never its entries or its chart. */
function summary(id: string, ticker: string, name: string) {
  return {
    id,
    name,
    ticker,
    from_date: '2026-04-15',
    to_date: '2026-05-15',
    entry_count: 2,
    has_chart: true,
    created_at_ms: Date.UTC(2026, 4, 15, 12),
    updated_at_ms: Date.UTC(2026, 4, 15, 12),
  };
}

/** The chart the setup was saved with: a document the Chart tab wrote, at 5-minute bars, with two quality gaps. */
const SAVED_CHART = {
  timeframe: '5m',
  bars: [{ t: 1_776_000_000_000, o: 1, h: 2, l: 0.5, c: 1.5, v: 10 }],
  indicators: [],
  quality: {
    raw_bar_count: 1,
    resampled_bar_count: 1,
    duplicates_removed: 0,
    gaps_found: 2,
    largest_gap_minutes: 30,
    missing_sessions: 0,
    session_coverage_pct: 99,
    synthetic_bars: 0,
    gap_details: [],
    missing_session_dates: [],
  },
  allowedTimeframes: ['1m', '5m', '1D'],
  estimatedBarsPerTimeframe: {},
  recommendedTimeframe: '5m',
  visibleIndicatorIds: ['ema|length=20'],
  barSources: null,
  warmupNote: null,
};

/** A saved setup whole, as Python stores it. */
function setup(id: string, ticker: string, overrides: Record<string, unknown> = {}) {
  return {
    ...summary(id, ticker, 'Apple review'),
    session: 'extended',
    forward_fill: false,
    adjusted: true,
    entries: [
      { name: 'ema', params: { length: 20 } },
      { name: 'rsi', params: { length: 14 } },
    ],
    chart_snapshot: SAVED_CHART,
    window_start_ms: Date.UTC(2026, 3, 15),
    window_end_ms: Date.UTC(2026, 4, 15),
    ...overrides,
  };
}

/** The list of saved setups, answered. */
async function answerList(opened: OpenedPlace, rows: readonly object[]): Promise<void> {
  const list = await waitFor(() => opened.http.expectOne((request) => request.url === SESSIONS_URL && request.method === 'GET'));
  list.flush(rows);
}

/** The one saved setup the page asked for whole, answered. */
async function answerSetup(opened: OpenedPlace, id: string, row: object): Promise<void> {
  const read = await waitFor(() => opened.http.expectOne(`${SESSIONS_URL}/${id}`));
  expect(read.request.method).toBe('GET');
  read.flush(row);
}

/** A place that has loaded its chart for a window other than the one a setup was saved with. */
async function openLoaded(url: string, extraRoutes = OLD_ADDRESSES) {
  const opened = await openPlace(url, [], extraRoutes);
  (await chartRequest(opened.http)).flush(chartResponse(500));
  await waitFor(() => expect(opened.store().chartStale()).toBe(false));
  return opened;
}

/** Proves the chart endpoint was asked for nothing: lets any deferred request run, then looks. */
async function expectNoChartRequest(opened: OpenedPlace): Promise<void> {
  opened.harness.fixture.detectChanges();
  await opened.harness.fixture.whenStable();
  await new Promise((resolve) => setTimeout(resolve));
  flushCatalog(opened.http);
  expect(opened.http.match(CHART_URL)).toHaveLength(0);
}

/** Proves opening only read: no setup was saved over, renamed or deleted. */
function expectRecordsUntouched(http: HttpTestingController): void {
  http.expectNone((request) => request.url.startsWith(SESSIONS_URL) && request.method !== 'GET');
}

async function openFromDrawer(opened: OpenedPlace, name: string, id: string, row: object, rows: readonly object[]) {
  await userEvent.click(screen.getByRole('button', { name: 'Saved setups' }));
  await answerList(opened, rows);
  await userEvent.click(await screen.findByRole('button', { name: new RegExp(name) }));
  await answerSetup(opened, id, row);
  await answerList(opened, rows);
}

describe('a saved Data Lab setup, under its instrument (T21)', () => {
  it('opens from the drawer with its recipe, its saved chart and its window, and asks the chart endpoint for nothing', async () => {
    const opened = await openLoaded(`/markets/SPY/chart?${ELSEWHERE}`);

    await openFromDrawer(opened, 'Apple review', SPY_SETUP, setup(SPY_SETUP, 'SPY'), [summary(SPY_SETUP, 'SPY', 'Apple review')]);

    await waitFor(() => expect(opened.router.url).toBe(OPENED_ADDRESS));
    // The recipe and the schema the record was opened under are the workspace's.
    expect(opened.store().indicators().map((indicator) => indicator.id)).toEqual(['ema|length=20', 'rsi|length=14']);
    expect(opened.store().savedSession()).toEqual({ id: SPY_SETUP, schemaVersion: 2 });
    expect(opened.store().committedScope()).toMatchObject({
      ticker: 'SPY',
      session: 'extended',
      forwardFill: false,
      adjusted: true,
      timespan: 'minute',
      multiplier: 5,
    });
    // The saved chart is what the tab shows: its own quality evidence, with no request behind it.
    expect(await screen.findByText(/Quality issues found: 2 total/)).toBeTruthy();
    await expectNoChartRequest(opened);
    expectRecordsUntouched(opened.http);
  });

  it('opens a setup saved for another instrument under that instrument', async () => {
    const opened = await openLoaded(`/markets/SPY/chart?${ELSEWHERE}`);

    await openFromDrawer(opened, 'Apple review', QQQ_SETUP, setup(QQQ_SETUP, 'QQQ'), [summary(QQQ_SETUP, 'QQQ', 'Apple review')]);

    await waitFor(() => expect(opened.router.url).toBe(OPENED_ADDRESS.replace('SPY', 'QQQ')));
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('QQQ');
    expect(opened.store().committedTicker()).toBe('QQQ');
    expect(opened.store().indicators().map((indicator) => indicator.id)).toEqual(['ema|length=20', 'rsi|length=14']);
    expect(await screen.findByText(/Quality issues found: 2 total/)).toBeTruthy();
    await expectNoChartRequest(opened);
  });

  it('opens from an address that names it, spends the id, and does not open it again', async () => {
    const opened = await openPlace(`/markets/SPY/chart?sessionId=${SPY_SETUP}`);

    await answerSetup(opened, SPY_SETUP, setup(SPY_SETUP, 'SPY'));
    await answerList(opened, [summary(SPY_SETUP, 'SPY', 'Apple review')]);

    await waitFor(() => expect(opened.router.url).toBe(OPENED_ADDRESS));
    expect(opened.store().indicators().map((indicator) => indicator.id)).toEqual(['ema|length=20', 'rsi|length=14']);
    expect(await screen.findByText(/Quality issues found: 2 total/)).toBeTruthy();
    await expectNoChartRequest(opened);
    // Back and Forward land on the address that has no id in it, so nothing reads the record again.
    opened.http.expectNone((request) => request.url.startsWith(SESSIONS_URL));
  });

  it('opens from the old Data Lab address that carried it, under the instrument its ticker names', async () => {
    const opened = await openPlace(`/data-lab?ticker=spy&${ELSEWHERE}&sessionId=${SPY_SETUP}`, [], OLD_ADDRESSES);

    await answerSetup(opened, SPY_SETUP, setup(SPY_SETUP, 'SPY'));
    await answerList(opened, [summary(SPY_SETUP, 'SPY', 'Apple review')]);

    await waitFor(() => expect(opened.router.url).toBe(OPENED_ADDRESS));
    expect(opened.store().savedSession()).toEqual({ id: SPY_SETUP, schemaVersion: 2 });
    expect(await screen.findByText(/Quality issues found: 2 total/)).toBeTruthy();
    await expectNoChartRequest(opened);
  });

  it('opens on the page the address named: a setup asked for from the old Build dataset address opens Build dataset', async () => {
    const opened = await openPlace(`/data-lab/export?ticker=spy&${ELSEWHERE}&sessionId=${SPY_SETUP}`, [], OLD_ADDRESSES);

    await answerSetup(opened, SPY_SETUP, setup(SPY_SETUP, 'SPY'));
    await answerList(opened, [summary(SPY_SETUP, 'SPY', 'Apple review')]);

    await waitFor(() => expect(opened.router.url).toBe(OPENED_ADDRESS.replace('/chart', '/data/build')));
    expect(await screen.findByText('SPY · 5 minute bars · extended session · 2 indicator instances')).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Data' }).getAttribute('aria-current')).toBe('page');
  });

  it('shows only the setup opened last when an earlier one is still being read', async () => {
    const opened = await openLoaded(`/markets/SPY/chart?${ELSEWHERE}`);
    const rows = [summary(SPY_SETUP, 'SPY', 'Apple review'), summary(QQQ_SETUP, 'QQQ', 'Tech review')];
    await userEvent.click(screen.getByRole('button', { name: 'Saved setups' }));
    await answerList(opened, rows);

    await userEvent.click(await screen.findByRole('button', { name: /Apple review/ }));
    const first = await waitFor(() => opened.http.expectOne(`${SESSIONS_URL}/${SPY_SETUP}`));
    await userEvent.click(await screen.findByRole('button', { name: /Tech review/ }));
    const second = await waitFor(() => opened.http.expectOne(`${SESSIONS_URL}/${QQQ_SETUP}`));
    second.flush(setup(QQQ_SETUP, 'QQQ', { name: 'Tech review', entries: [{ name: 'ema', params: { length: 10 } }] }));
    first.flush(setup(SPY_SETUP, 'SPY'));
    await answerList(opened, rows);

    await waitFor(() => expect(opened.router.url).toBe(OPENED_ADDRESS.replace('SPY', 'QQQ')));
    expect(opened.store().indicators().map((indicator) => indicator.id)).toEqual(['ema|length=10']);
    expect(opened.store().savedSession()).toEqual({ id: QQQ_SETUP, schemaVersion: 2 });
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('QQQ');
  });

  it('opens a setup under the instrument the record names, not the one the address claimed', async () => {
    const opened = await openPlace(`/markets/SPY/chart?sessionId=${QQQ_SETUP}`);

    await answerSetup(opened, QQQ_SETUP, setup(QQQ_SETUP, 'QQQ'));
    await answerList(opened, [summary(QQQ_SETUP, 'QQQ', 'Apple review')]);

    await waitFor(() => expect(opened.router.url).toBe(OPENED_ADDRESS.replace('SPY', 'QQQ')));
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('QQQ');
  });

  it('says what a record cannot give, keeps the rest, loads the chart fresh, and writes nothing back', async () => {
    const opened = await openLoaded(`/markets/SPY/chart?${ELSEWHERE}`);
    const legacy = setup(SPY_SETUP, 'SPY', {
      entries: [{ name: 'ema', params: { length: 20 } }, { name: 'rsi' }, 'junk'],
    });

    await openFromDrawer(opened, 'Apple review', SPY_SETUP, legacy, [summary(SPY_SETUP, 'SPY', 'Apple review')]);

    const notice = await screen.findByText('Saved setup.');
    const notes = within(notice.closest('.migration-notice') as HTMLElement);
    expect(notes.getByText('Dropped indicator entry "rsi" with invalid params')).toBeTruthy();
    expect(notes.getByText('Dropped invalid indicator entry (not an object)')).toBeTruthy();
    // What it could take is intact, and the chart is asked for, with only that recipe, because the saved one answered another.
    expect(opened.store().indicators().map((indicator) => indicator.id)).toEqual(['ema|length=20']);
    expect(opened.store().committedScope()).toMatchObject({ ticker: 'SPY', session: 'extended', timespan: 'minute', multiplier: 5 });
    const request = await chartRequest(opened.http);
    expect(request.request.body['indicators']).toEqual([{ name: 'ema', params: { length: 20 } }]);
    expect(request.request.body['timeframe']).toBe('5m');
    request.flush(chartResponse(500));
    expectRecordsUntouched(opened.http);
  });

  it('says when no such setup is held, and opens the page the address names', async () => {
    const opened = await openPlace(`/markets/SPY/chart?${ELSEWHERE}&sessionId=${SPY_SETUP}`);

    (await waitFor(() => opened.http.expectOne(`${SESSIONS_URL}/${SPY_SETUP}`))).flush(
      { detail: { code: 'DATA_LAB_SESSION_NOT_FOUND', message: `Session ${SPY_SETUP} not found` } },
      { status: 404, statusText: 'Not Found' },
    );
    await answerList(opened, []);

    await waitFor(() => expect(opened.router.url).toBe(`/markets/SPY/chart?${ELSEWHERE}`));
    const request = await chartRequest(opened.http);
    expect(request.request.body).toMatchObject({ ticker: 'SPY', start_ms_utc: Date.UTC(2026, 0, 2) });
    request.flush(chartResponse(500));
  });

  it('names an id that is not a setup id, and opens the page the address names', async () => {
    const opened = await openPlace(`/markets/SPY/chart?${ELSEWHERE}&sessionId=not-an-id`);

    expect(await screen.findByText(/Ignored sessionId "not-an-id"/)).toBeTruthy();
    await waitFor(() => expect(opened.router.url).toBe(`/markets/SPY/chart?${ELSEWHERE}`));
    opened.http.expectNone((request) => request.url.startsWith(SESSIONS_URL));
    (await chartRequest(opened.http)).flush(chartResponse(500));
  });
});

describe('a legacy Data Lab address with fields this page does not read', () => {
  it('opens its instrument and window as it named them, and names what it ignored', async () => {
    const opened = await openPlace(
      `/data-lab?ticker=SPY&${ELSEWHERE}&trading-session=extended&theme=dark&indicators=ema`,
      [],
      OLD_ADDRESSES,
    );

    const request = await chartRequest(opened.http);
    expect(request.request.body).toMatchObject({
      ticker: 'SPY',
      start_ms_utc: Date.UTC(2026, 0, 2),
      end_ms_utc: Date.UTC(2026, 1, 27) + 86_400_000 - 1,
      session: 'extended',
    });
    const notice = (await screen.findByText('Migrated link.')).closest('.migration-notice') as HTMLElement;
    expect(within(notice).getByText('Ignored unknown query key "theme"')).toBeTruthy();
    expect(within(notice).getByText('Ignored unknown query key "indicators"')).toBeTruthy();
    expect(within(notice).queryByText(/"ticker"|"from"|"to"|"trading-session"/), 'what it kept is not reported').toBeNull();
    expect(opened.router.url).toBe(`/markets/SPY/chart?${ELSEWHERE}&theme=dark&indicators=ema&session=extended`);
    request.flush(chartResponse(500));

    await userEvent.click(within(notice).getByRole('button', { name: 'Dismiss' }));
    expect(screen.queryByText('Migrated link.')).toBeNull();
  });

  it('says nothing about an address whose every key the page reads', async () => {
    const opened = await openPlace(`/markets/SPY/chart?${ELSEWHERE}&timeframe=1h&session=extended`);

    (await chartRequest(opened.http)).flush(chartResponse(500));
    expect(screen.queryByText('Migrated link.')).toBeNull();
  });
});

describe('saving a setup under the instrument', () => {
  const posts = (request: HttpRequest<unknown>) => request.method === 'POST' && request.url === SESSIONS_URL;

  it('saves the committed scope and recipe, and the chart while it still answers them', async () => {
    const opened = await openLoaded(`/markets/SPY/chart?${ELSEWHERE}&timeframe=1h`);

    await userEvent.click(screen.getByRole('button', { name: 'Saved setups' }));
    await answerList(opened, []);
    await userEvent.type(screen.getByPlaceholderText('Setup name'), 'Hourly look');
    await userEvent.click(screen.getByRole('button', { name: 'Save setup' }));

    const save = await waitFor(() => opened.http.expectOne(posts));
    expect(save.request.body).toMatchObject({
      name: 'Hourly look',
      ticker: 'SPY',
      from_date: '2026-01-02',
      to_date: '2026-02-27',
      session: 'rth',
      forward_fill: true,
      adjusted: true,
    });
    expect(save.request.body['entries']).toHaveLength(13);
    expect(save.request.body['chart_snapshot']).toMatchObject({ timeframe: '1h' });
    save.flush({ id: SPY_SETUP });
    await answerList(opened, [summary(SPY_SETUP, 'SPY', 'Hourly look')]);

    expect(await screen.findByRole('button', { name: 'Update setup' })).toBeTruthy();
    expect(opened.store().savedSession()).toEqual({ id: SPY_SETUP, schemaVersion: 2 });
  });

  it('saves no chart once the committed context has moved past the one on screen', async () => {
    const opened = await openLoaded(`/markets/SPY/chart?${ELSEWHERE}`);
    // A new window is committed; its chart has not answered yet.
    opened.context().commitWindow({ from: '2026-03-02', to: '2026-03-31' });
    await chartRequest(opened.http);

    await userEvent.click(screen.getByRole('button', { name: 'Saved setups' }));
    await answerList(opened, []);
    await userEvent.click(screen.getByRole('button', { name: 'Save setup' }));

    const save = await waitFor(() => opened.http.expectOne(posts));
    expect(save.request.body).toMatchObject({ from_date: '2026-03-02', to_date: '2026-03-31', chart_snapshot: null });
  });

  it('never saves over another instrument\'s setup', async () => {
    const opened = await openLoaded(`/markets/SPY/chart?${ELSEWHERE}`);
    await openFromDrawer(opened, 'Apple review', QQQ_SETUP, setup(QQQ_SETUP, 'QQQ'), [summary(QQQ_SETUP, 'QQQ', 'Apple review')]);
    await waitFor(() => expect(opened.router.url).toContain('/markets/QQQ/'));
    await userEvent.click(screen.getByRole('button', { name: 'Saved setups' }));
    await answerList(opened, [summary(QQQ_SETUP, 'QQQ', 'Apple review')]);
    expect(await screen.findByRole('button', { name: 'Update setup' }), 'on its own instrument it updates the setup').toBeTruthy();

    // The place moves to another instrument with the setup still the workspace's reference.
    opened.context().switchSymbol('SPY');
    await waitFor(() => expect(opened.router.url).toContain('/markets/SPY/'));

    expect(await screen.findByRole('button', { name: 'Save setup' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Update setup' })).toBeNull();
    expectRecordsUntouched(opened.http);
  });
});
