import { screen, waitFor } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { of } from 'rxjs';
import { describe, expect, it, vi } from 'vitest';

import { environment } from '../../environments/environment';
import { TradeBackfillRunner } from '../shared/data-lake/trade-backfill-runner';
import { ReturnsDistributionService } from './returns/returns-distribution.service';
import {
  INSTRUMENT_DATA_URL,
  WINDOW_QUERY,
  dataReport,
  dataSession,
  flushCatalog,
  openPlace,
  type OpenedPlace,
} from './testing/instrument-place';
import { STUDY } from './testing/returns-study';

// The canvas libraries are not exercised: what each tab asked for and showed is.
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

vi.mock('chart.js', () => ({
  Chart: class {
    static register(..._args: unknown[]): void {}
    destroy(): void {}
  },
  registerables: [],
}));

const CHART_URL = `${environment.pythonServiceUrl}/api/chart/data`;

function chartResponse() {
  return {
    bars: [{ t: 1_764_167_400_000, o: 500, h: 500, l: 500, c: 500, v: 1000 }],
    indicators: [],
    quality: {
      raw_bar_count: 1,
      resampled_bar_count: 1,
      duplicates_removed: 0,
      gaps_found: 0,
      largest_gap_minutes: 0,
      missing_sessions: 0,
      session_coverage_pct: 100,
      synthetic_bars: 0,
      gap_details: [],
      missing_session_dates: [],
    },
    bar_sources: null,
    allowed_timeframes: ['1m', '5m', '1D'],
    estimated_bars_per_timeframe: {},
    recommended_timeframe: '1D',
    meta: { cached_resample: false, cached_indicators: false },
  };
}

/** Everything the place asked of Python for these tabs, so a tab change can be shown to have asked for nothing. */
function world() {
  const returns = { distribution: vi.fn(() => of(STUDY)), minuteCandles: vi.fn(() => of([])) };
  const backfill = { run: vi.fn() };
  return {
    returns,
    backfill,
    providers: [
      { provide: ReturnsDistributionService, useValue: returns },
      { provide: TradeBackfillRunner, useValue: backfill },
    ],
  };
}

async function openChart(providers: ReturnType<typeof world>['providers']): Promise<OpenedPlace> {
  const opened = await openPlace(`/markets/SPY/chart?${WINDOW_QUERY}`, providers);
  const [chart] = await waitFor(() => {
    flushCatalog(opened.http);
    const open = opened.http.match(CHART_URL);
    expect(open).toHaveLength(1);
    return open;
  });
  chart.flush(chartResponse());
  await waitFor(() => expect(opened.store().chartStale()).toBe(false));
  return opened;
}

/** The person's Run on the Returns tab: the study fetches missing sessions into the lake, so opening the tab runs nothing. */
async function runStudy(): Promise<void> {
  await userEvent.click(await screen.findByRole('button', { name: 'Run the study' }));
}

async function goTo(opened: OpenedPlace, tab: 'Chart' | 'Returns' | 'Data'): Promise<void> {
  await userEvent.click(screen.getByRole('link', { name: tab }));
  await waitFor(() => expect(opened.router.url).toContain(`/${tab.toLowerCase()}`));
}

/** Lets anything a tab defers run, then reports what the data plane was asked, other than the catalog read every chart starts. */
async function settle(opened: OpenedPlace): Promise<void> {
  opened.harness.fixture.detectChanges();
  await opened.harness.fixture.whenStable();
  await new Promise((resolve) => setTimeout(resolve));
  flushCatalog(opened.http);
}

describe('changing tabs in an instrument\'s place (T03)', () => {
  it('keeps the instrument and the window through Chart, Returns and Data, and asks for nothing it already has', async () => {
    const { returns, backfill, providers } = world();
    const opened = await openChart(providers);

    await goTo(opened, 'Returns');
    // Opening the tab runs nothing; the study runs when the person presses Run.
    expect(await screen.findByRole('button', { name: 'Run the study' })).toBeTruthy();
    expect(returns.distribution).not.toHaveBeenCalled();
    await runStudy();
    expect(await screen.findByText('Days studied')).toBeTruthy();
    expect(returns.distribution).toHaveBeenCalledTimes(1);
    expect(returns.distribution).toHaveBeenCalledWith({
      symbol: 'SPY',
      fromMsUtc: Date.UTC(2026, 3, 15),
      toMsUtc: Date.UTC(2026, 4, 15) + 86_400_000 - 1,
      binWidthPct: 0.5,
      spanPct: 5,
    });

    await goTo(opened, 'Data');
    const read = await waitFor(() => opened.http.expectOne((request) => request.url === INSTRUMENT_DATA_URL));
    read.flush(dataReport([dataSession(0, 'ready'), dataSession(1, 'ready')]));
    expect(await screen.findByText('A run can read every finished session in this window.')).toBeTruthy();

    // Around again: the window is the one committed, and every tab shows what it held.
    await goTo(opened, 'Chart');
    expect(opened.router.url).toBe(`/markets/SPY/chart?${WINDOW_QUERY}`);
    await settle(opened);
    expect(await screen.findByText(/Quality clean/)).toBeTruthy();
    await goTo(opened, 'Returns');
    expect(await screen.findByText('Days studied')).toBeTruthy();
    await goTo(opened, 'Data');
    expect(await screen.findByText('A run can read every finished session in this window.')).toBeTruthy();
    await settle(opened);

    expect(opened.router.url).toBe(`/markets/SPY/data?${WINDOW_QUERY}`);
    expect(returns.distribution, 'the study ran once for one context').toHaveBeenCalledTimes(1);
    opened.http.expectNone(CHART_URL);
    opened.http.expectNone((request) => request.url === INSTRUMENT_DATA_URL);
    expect(backfill.run, 'opening a tab fills nothing').not.toHaveBeenCalled();
    opened.http.verify();
  });

  it('asks each tab once more when the committed window changes, and never fills', async () => {
    const { returns, backfill, providers } = world();
    const opened = await openChart(providers);
    await goTo(opened, 'Returns');
    await runStudy();
    expect(await screen.findByText('Days studied')).toBeTruthy();
    await goTo(opened, 'Data');
    (await waitFor(() => opened.http.expectOne((request) => request.url === INSTRUMENT_DATA_URL))).flush(
      dataReport([dataSession(0, 'ready')]),
    );
    await screen.findByText('A run can read every finished session in this window.');

    opened.context().commitWindow({ from: '2026-04-20', to: '2026-05-20' });
    const reread = await waitFor(() => opened.http.expectOne((request) => request.url === INSTRUMENT_DATA_URL));
    expect(reread.request.params.get('start_trading_date_ms')).toBe(String(Date.UTC(2026, 3, 20, 12)));
    reread.flush(dataReport([dataSession(0, 'ready')]));

    // The window the study was run for has moved on: the tab waits for its own Run.
    await goTo(opened, 'Returns');
    expect(await screen.findByRole('button', { name: 'Run the study' })).toBeTruthy();
    expect(returns.distribution).toHaveBeenCalledTimes(1);
    await runStudy();
    await waitFor(() => expect(returns.distribution).toHaveBeenCalledTimes(2));
    expect(returns.distribution).toHaveBeenLastCalledWith(
      expect.objectContaining({ fromMsUtc: Date.UTC(2026, 3, 20), toMsUtc: Date.UTC(2026, 4, 20) + 86_400_000 - 1 }),
    );
    expect(backfill.run).not.toHaveBeenCalled();
    // The chart sat out those changes, and reads the new window when it is opened again.
    await goTo(opened, 'Chart');
    await waitFor(() => {
      flushCatalog(opened.http);
      expect(opened.http.match(CHART_URL)).toHaveLength(1);
    });
  });

  it('runs nothing for a half-typed or reversed window, on the tab it is entered under', async () => {
    const { returns, providers } = world();
    const opened = await openChart(providers);
    await goTo(opened, 'Returns');
    await runStudy();
    await screen.findByText('Days studied');

    // Half a date is never committed: the address and the study stay as they were.
    expect(opened.context().commitWindow({ from: '2026-05-01', to: '' })).toBe(false);
    expect(opened.context().commitWindow({ from: '', to: '' })).toBe(false);
    await settle(opened);
    expect(opened.router.url).toBe(`/markets/SPY/returns?${WINDOW_QUERY}`);
    expect(returns.distribution).toHaveBeenCalledTimes(1);

    // A reversed range reaches the address, which says it cannot be used and shows no tab to run.
    opened.context().commitWindow({ from: '2026-05-20', to: '2026-05-01' });
    expect(await screen.findByText('The start date is after the end date.')).toBeTruthy();
    expect(screen.queryByText('Days studied')).toBeNull();
    await settle(opened);
    expect(returns.distribution).toHaveBeenCalledTimes(1);
  });

  it('keeps the reading it holds when a tab is left mid-read, and does not read twice for one request', async () => {
    const { providers } = world();
    const opened = await openPlace(`/markets/SPY/data?${WINDOW_QUERY}`, providers);
    const first = await waitFor(() => opened.http.expectOne((request) => request.url === INSTRUMENT_DATA_URL));

    await goTo(opened, 'Returns');
    await runStudy();
    await screen.findByText('Days studied');
    await goTo(opened, 'Data');
    // The first read is still open; the second visit asks nothing more for the same request.
    opened.http.expectNone((request) => request.url === INSTRUMENT_DATA_URL);
    first.flush(dataReport([dataSession(0, 'ready')]));

    expect(await screen.findByText('A run can read every finished session in this window.')).toBeTruthy();
  });
});
