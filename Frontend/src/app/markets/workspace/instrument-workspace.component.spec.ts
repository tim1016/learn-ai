import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting, type TestRequest } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { By } from '@angular/platform-browser';
import { Router, provideRouter, withComponentInputBinding, withRouterConfig } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';
import { fireEvent, screen, waitFor } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { environment } from '../../../environments/environment';
import { DataLabRangePresetsService, type ChartRangePreset } from '../../services/data-lab-range-presets.service';
import { DataLabWorkspaceStore } from '../../components/data-lab/data-lab-workspace-store';
import { fakePickerWorld, pickSymbol } from '../../shared/symbol-picker/testing/fake-picker-world';
import type { VendorSymbolEntry } from '../../shared/symbol-catalog/vendor-catalog.service';
import { DataLabChartComponent } from '../chart/data-lab-chart/data-lab-chart.component';
import { InstrumentContextService } from '../instrument-context.service';
import { MARKETS_ROUTES } from '../markets.routes';
import { InstrumentWorkspaceComponent } from './instrument-workspace.component';

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

const CHART_URL = `${environment.pythonServiceUrl}/api/chart/data`;
const NEWS_URL = `${environment.pythonServiceUrl}/api/news`;

const FROM = '2026-04-15';
const TO = '2026-05-15';
const DEFAULT_WINDOW = { from: '2025-09-22', to: '2026-09-18' };
const WINDOW_QUERY = `from=${FROM}&to=${TO}`;

/** Python's 1Y range, as the workspace reads it for an address with no window. */
const PRESET_1Y: ChartRangePreset = {
  key: '1Y',
  label: 'Past year',
  start_ms_utc: Date.UTC(2025, 8, 22),
  end_ms_utc: Date.UTC(2026, 8, 18) + 86_400_000 - 1,
  session_count: 251,
  estimated_bars_per_timeframe: { '1D': 251 },
};

const vendor = (symbol: string, status: 'active' | 'inactive' = 'active'): VendorSymbolEntry => ({
  symbol,
  name: `${symbol} Inc.`,
  exchange: 'XNAS',
  asset_class: 'us_equity',
  status,
});

/** The lake holds SPY and QQQ; the vendor also lists NVDA and AMD, which the lake does not hold. */
function world() {
  return fakePickerWorld(
    [
      { symbol: 'SPY', name: 'SPDR S&P 500', firstHeld: '2024-01-02', lastHeld: '2026-09-18' },
      { symbol: 'QQQ', name: 'Invesco QQQ', firstHeld: '2024-01-02', lastHeld: '2026-09-18' },
    ],
    [vendor('SPY'), vendor('QQQ'), vendor('NVDA'), vendor('AMD')],
  );
}

function chartResponse(close: number) {
  return {
    bars: [{ t: 1_764_167_400_000, o: close, h: close, l: close, c: close, v: 1000 }],
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

async function open(url: string, pickers = world(), presets: () => Promise<readonly ChartRangePreset[]> = async () => [PRESET_1Y]) {
  TestBed.configureTestingModule({
    providers: [
      provideRouter(
        [{ path: 'markets', loadChildren: () => MARKETS_ROUTES }],
        withComponentInputBinding(),
        withRouterConfig({ paramsInheritanceStrategy: 'always' }),
      ),
      provideHttpClient(),
      provideHttpClientTesting(),
      ...pickers.providers,
      { provide: DataLabRangePresetsService, useValue: { presets: vi.fn(presets) } },
    ],
  });
  const harness = await RouterTestingHarness.create();
  const http = TestBed.inject(HttpTestingController);
  const router = TestBed.inject(Router);
  await harness.navigateByUrl(url);
  harness.fixture.detectChanges();
  const workspace = () => harness.fixture.debugElement.query(By.directive(InstrumentWorkspaceComponent));
  return {
    harness,
    http,
    router,
    pickers,
    context: () => workspace().injector.get(InstrumentContextService),
    store: () => workspace().injector.get(DataLabWorkspaceStore),
  };
}

type Opened = Awaited<ReturnType<typeof open>>;

/** The catalog read every chart tab starts; its answer is empty and beside the point. */
function flushCatalog(http: HttpTestingController): void {
  http.match(`${environment.pythonServiceUrl}/api/dataset/available`).forEach((request) =>
    request.flush({ success: true, categories: {}, total: 0 }),
  );
}

/** Lets the deferred request run, then reports exactly the chart requests open. */
async function chartRequests(http: HttpTestingController, count: number): Promise<TestRequest[]> {
  return waitFor(() => {
    flushCatalog(http);
    const matches = http.match(CHART_URL);
    expect(matches).toHaveLength(count);
    return matches;
  });
}

/** Proves nothing else was asked for: lets any deferred request run, then looks. */
async function expectNoChartRequest(opened: Opened): Promise<void> {
  opened.harness.fixture.detectChanges();
  await opened.harness.fixture.whenStable();
  await new Promise((resolve) => setTimeout(resolve));
  flushCatalog(opened.http);
  expect(opened.http.match(CHART_URL)).toHaveLength(0);
}

async function openLoaded(url: string, close = 500) {
  const opened = await open(url);
  const [first] = await chartRequests(opened.http, 1);
  first.flush(chartResponse(close));
  await waitFor(() => expect(opened.store().chartStale()).toBe(false));
  return opened;
}

describe('an instrument\'s place', () => {
  describe('opening', () => {
    it('opens the chart for the address\'s instrument, window and bar policy, and names the instrument', async () => {
      const opened = await open(`/markets/SPY/chart?${WINDOW_QUERY}&timeframe=5m&session=extended`);
      const [request] = await chartRequests(opened.http, 1);

      expect(request.request.body).toMatchObject({
        ticker: 'SPY',
        start_ms_utc: Date.UTC(2026, 3, 15),
        end_ms_utc: Date.UTC(2026, 4, 15) + 86_400_000 - 1,
        timeframe: '5m',
        session: 'extended',
      });
      expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('SPY');
      expect(screen.getByRole('link', { name: 'Markets' }).getAttribute('href')).toBe('/markets');
      request.flush(chartResponse(500));
      opened.http.verify();
    });

    it('reaches the chart from the instrument alone, keeping the query', async () => {
      const opened = await open(`/markets/SPY?${WINDOW_QUERY}`);

      expect(opened.router.url).toBe(`/markets/SPY/chart?${WINDOW_QUERY}`);
      await chartRequests(opened.http, 1);
    });

    it('declares Chart, Returns, Data and Options once; only Options is not routed yet, so it stays in the strip, inert, and says why', async () => {
      const opened = await open(`/markets/SPY/chart?${WINDOW_QUERY}`);
      await chartRequests(opened.http, 1);

      const strip = screen.getByRole('navigation', { name: 'Instrument sections' });
      expect(Array.from(strip.children).map((tab) => tab.textContent?.trim().split(' —')[0].trim())).toEqual([
        'Chart',
        'Returns',
        'Data',
        'Options',
      ]);
      for (const name of ['Chart', 'Returns', 'Data']) {
        const tab = name.toLowerCase();
        expect(screen.getByRole('link', { name }).getAttribute('href'), `${name} keeps the window`).toBe(`/markets/SPY/${tab}?${WINDOW_QUERY}`);
      }
      expect(screen.queryByRole('link', { name: 'Options' }), 'Options is not a link').toBeNull();
      expect(screen.getByText(/^Options/).getAttribute('title'), 'Options says why').toContain('Not in this place yet');
    });
  });

  describe('the window an address names none of', () => {
    it('opens on the default range Python resolved, shows it, and leaves the address alone until something is committed', async () => {
      const opened = await open('/markets/SPY/chart');
      const [request] = await chartRequests(opened.http, 1);

      expect(request.request.body['start_ms_utc']).toBe(PRESET_1Y.start_ms_utc);
      expect(request.request.body['end_ms_utc']).toBe(PRESET_1Y.end_ms_utc);
      expect(screen.getByRole<HTMLInputElement>('textbox', { name: 'Start date' }).value).toBe(DEFAULT_WINDOW.from);
      expect(screen.getByRole<HTMLInputElement>('textbox', { name: 'End date' }).value).toBe(DEFAULT_WINDOW.to);
      expect(opened.router.url).toBe('/markets/SPY/chart');
      request.flush(chartResponse(500));

      // The first thing committed over it pins the window the place opened on.
      await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Timeframe' }), '1h');
      await waitFor(() => expect(opened.router.url).toBe(`/markets/SPY/chart?timeframe=1h&session=regular&from=${DEFAULT_WINDOW.from}&to=${DEFAULT_WINDOW.to}`));
    });
  });

  describe('when Python\'s ranges cannot give the window an address names none of', () => {
    it.each([
      ['cannot be read', async () => { throw new Error('The data plane is down.'); }, /The data plane is down\./],
      ['hold no 1Y range', async () => [] as readonly ChartRangePreset[], /1Y range was not among the ranges Python resolved/],
    ])('says so when they %s, starts no request, and opens on a window picked by hand', async (_name, presets, reason) => {
      const opened = await open('/markets/SPY/chart', world(), presets);

      expect(await screen.findByText('No default window could be read for this instrument.')).toBeTruthy();
      expect(screen.getAllByText(reason).length).toBeGreaterThan(0);
      await expectNoChartRequest(opened);

      fireEvent.change(screen.getByRole('textbox', { name: 'Start date' }), { target: { value: '2026-04-01' } });
      fireEvent.change(screen.getByRole('textbox', { name: 'End date' }), { target: { value: '2026-04-30' } });
      const [request] = await chartRequests(opened.http, 1);
      expect(request.request.body).toMatchObject({ ticker: 'SPY', start_ms_utc: Date.UTC(2026, 3, 1) });
    });
  });

  describe('committing the context (T03)', () => {
    it.each([
      ['a half-typed date', '2026-0'],
      ['a reversed range', '2026-06-01'],
      ['a date that does not exist', '2026-13-45'],
    ])('starts no request for %s', async (_name, typed) => {
      const opened = await openLoaded(`/markets/SPY/chart?${WINDOW_QUERY}`);

      fireEvent.change(screen.getByRole('textbox', { name: 'Start date' }), { target: { value: typed } });

      await expectNoChartRequest(opened);
      expect(opened.router.url).toBe(`/markets/SPY/chart?${WINDOW_QUERY}`);
      opened.http.verify();
    });

    it('loads one valid change once, and the same entry again loads nothing', async () => {
      const opened = await openLoaded(`/markets/SPY/chart?${WINDOW_QUERY}`);
      const start = screen.getByRole('textbox', { name: 'Start date' });

      fireEvent.change(start, { target: { value: '2026-04-20' } });
      const [request] = await chartRequests(opened.http, 1);
      expect(request.request.body['start_ms_utc']).toBe(Date.UTC(2026, 3, 20));
      expect(opened.router.url).toBe(`/markets/SPY/chart?from=2026-04-20&to=${TO}&timeframe=1d&session=regular`);
      request.flush(chartResponse(500));
      await waitFor(() => expect(opened.store().chartStale()).toBe(false));

      fireEvent.change(start, { target: { value: '2026-04-20' } });
      expect(opened.context().commitWindow({ from: '2026-04-20', to: TO })).toBe(false);
      await expectNoChartRequest(opened);
      opened.http.verify();
    });

    it('loads a changed timeframe once and a repeated one not at all', async () => {
      const opened = await openLoaded(`/markets/SPY/chart?${WINDOW_QUERY}`);

      await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Timeframe' }), '15m');
      const [request] = await chartRequests(opened.http, 1);
      expect(request.request.body['timeframe']).toBe('15m');
      request.flush(chartResponse(500));
      await waitFor(() => expect(opened.store().chartStale()).toBe(false));

      expect(opened.context().commitTimeframe('15m')).toBe(false);
      await expectNoChartRequest(opened);
    });

    it('commits a quick range as Python resolved it, with its own intraday timeframe', async () => {
      const opened = await openLoaded(`/markets/SPY/chart?${WINDOW_QUERY}`);
      const oneDay: ChartRangePreset = { ...PRESET_1Y, key: '1D', start_ms_utc: Date.UTC(2026, 8, 18), end_ms_utc: Date.UTC(2026, 8, 18) + 86_400_000 - 1 };
      TestBed.inject(DataLabRangePresetsService).presets = vi.fn(async () => [oneDay, PRESET_1Y]);

      await opened.context().commitPreset('1D');

      const [request] = await chartRequests(opened.http, 1);
      expect(request.request.body).toMatchObject({ start_ms_utc: oneDay.start_ms_utc, end_ms_utc: oneDay.end_ms_utc, timeframe: '5m' });
    });

    it('never shows one instrument\'s answer under another when the instrument changes mid-load', async () => {
      const opened = await open(`/markets/SPY/chart?${WINDOW_QUERY}`);
      const [spy] = await chartRequests(opened.http, 1);

      // SPY is still loading when QQQ is picked in the header.
      pickSymbol(opened.harness.fixture, 'QQQ');
      await waitFor(() => expect(opened.router.url).toBe(`/markets/QQQ/chart?${WINDOW_QUERY}`));
      const [qqq] = await chartRequests(opened.http, 1);

      expect(spy.cancelled).toBe(true);
      expect(qqq.request.body['ticker']).toBe('QQQ');
      qqq.flush(chartResponse(400));
      await waitFor(() => expect(opened.store().chartStale()).toBe(false));

      const shown = opened.store().latestChartSnapshot() as { bars: { c: number }[] };
      expect(shown.bars.map((bar) => bar.c)).toEqual([400]);
      expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('QQQ');
      opened.http.verify();
    });

    it('shows nothing of one instrument\'s chart under another\'s name while the other loads', async () => {
      const opened = await openLoaded(`/markets/SPY/chart?${WINDOW_QUERY}`, 500);
      const chart = () => opened.harness.fixture.debugElement.query(By.directive(DataLabChartComponent)).componentInstance as DataLabChartComponent;
      expect(chart().hasData()).toBe(true);

      pickSymbol(opened.harness.fixture, 'QQQ');
      await waitFor(() => expect(opened.router.url).toBe(`/markets/QQQ/chart?${WINDOW_QUERY}`));
      const [qqq] = await chartRequests(opened.http, 1);

      expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('QQQ');
      expect(chart().hasData()).toBe(false);
      expect(chart().quality()).toBeNull();
      qqq.flush(chartResponse(400));
      await waitFor(() => expect(chart().hasData()).toBe(true));
    });

    it('keeps the window and bar policy through a move to another instrument', async () => {
      const opened = await openLoaded(`/markets/SPY/chart?${WINDOW_QUERY}&timeframe=15m&session=extended`);

      pickSymbol(opened.harness.fixture, 'QQQ');

      await waitFor(() => expect(opened.router.url).toBe(`/markets/QQQ/chart?${WINDOW_QUERY}&timeframe=15m&session=extended`));
      const [request] = await chartRequests(opened.http, 1);
      expect(request.request.body).toMatchObject({ ticker: 'QQQ', timeframe: '15m', session: 'extended' });
    });

    it('refuses an address whose window it cannot use, says why, starts no request, and takes a corrected window', async () => {
      const opened = await open('/markets/SPY/chart?from=2026-05-15&to=2026-04-15');

      expect(await screen.findByText(/start date is after the end date/)).toBeTruthy();
      await expectNoChartRequest(opened);

      fireEvent.change(screen.getByRole('textbox', { name: 'Start date' }), { target: { value: '2026-04-01' } });
      fireEvent.change(screen.getByRole('textbox', { name: 'End date' }), { target: { value: '2026-04-30' } });

      const [request] = await chartRequests(opened.http, 1);
      expect(request.request.body).toMatchObject({ ticker: 'SPY', start_ms_utc: Date.UTC(2026, 3, 1) });
    });
  });

  describe('changing tabs keeps the context', () => {
    it('keeps the window through the headlines page and back, without asking for the chart again', async () => {
      const opened = await openLoaded(`/markets/SPY/chart?${WINDOW_QUERY}&timeframe=5m`);

      await userEvent.click(screen.getByRole('button', { name: /Headlines for SPY/ }));
      opened.http.expectOne((r) => r.url === NEWS_URL).flush({ articles: [], count: 0, vendor: 'polygon', fetched_at_ms: 1 });
      await userEvent.click(await screen.findByRole('link', { name: 'View all' }));

      await waitFor(() => expect(opened.router.url).toBe(`/markets/SPY/chart/news?${WINDOW_QUERY}&timeframe=5m`));
      const page = await waitFor(() => opened.http.expectOne((r) => r.url === NEWS_URL));
      expect(page.request.params.get('ticker')).toBe('SPY');
      page.flush({ articles: [], count: 0, vendor: 'polygon', fetched_at_ms: 1 });

      await userEvent.click(await screen.findByRole('link', { name: 'Back to the SPY chart' }));
      await waitFor(() => expect(opened.router.url).toBe(`/markets/SPY/chart?${WINDOW_QUERY}&timeframe=5m`));

      // The same context: the chart shows the answer it held and asks nothing.
      await expectNoChartRequest(opened);
      expect(await screen.findByText(/Quality clean/)).toBeTruthy();
      opened.http.verify();
    });
  });

  describe('an address the lake cannot open', () => {
    it.each([
      ['an instrument nothing lists', 'ZZZZ', 'ZZZZ', /not in the lake and is not a listed US-equity symbol/],
      ['something that is not a symbol', '%24%24%24', '$$$', /is not a symbol this place can open/],
    ])('fails in place for %s, with the way back, and opens nothing in its place', async (_name, typed, shown, reason) => {
      const opened = await open(`/markets/${typed}/chart?${WINDOW_QUERY}`);

      expect(await screen.findByText(reason)).toBeTruthy();
      expect(screen.getByRole('link', { name: 'Back to Markets' }).getAttribute('href')).toBe('/markets');
      // It stays at the address it was given: nothing replaced it with another instrument.
      expect(opened.router.url).toBe(`/markets/${shown}/chart?${WINDOW_QUERY}`);
      expect(screen.queryByRole('navigation', { name: 'Instrument sections' })).toBeNull();
      await expectNoChartRequest(opened);
      expect(opened.pickers.coverage.ensureCalls).toEqual([]);
    });

    it('says it does not know, and offers to read the catalog again, while the catalog cannot answer', async () => {
      const pickers = world();
      pickers.vendor.unavailable.set('The vendor catalog is down.');
      const opened = await open('/markets/ZZZZ/chart', pickers);

      expect(await screen.findByText(/Whether ZZZZ can be opened is not known/)).toBeTruthy();
      expect(screen.getByText('The vendor catalog is down.')).toBeTruthy();
      await userEvent.click(screen.getByRole('button', { name: 'Read the catalog again' }));
      expect(pickers.vendor.reloadCount).toBe(1);
      await expectNoChartRequest(opened);
    });
  });

  describe('an instrument the lake does not hold yet (T04)', () => {
    it('fills it through the shared gate, says so, and opens it when the lake confirms the bars', async () => {
      const pickers = world();
      pickers.coverage.holdNext = true;
      const opened = await open(`/markets/NVDA/chart?${WINDOW_QUERY}`, pickers);

      expect(await screen.findByText(/NVDA is not in the lake\. Its trade bars are being filled/)).toBeTruthy();
      expect(pickers.coverage.ensureCalls).toEqual([{ symbol: 'NVDA', mode: 'polygon_split_adjusted' }]);
      await expectNoChartRequest(opened);

      pickers.coverage.held[0].resolve(true);
      const [request] = await chartRequests(opened.http, 1);
      expect(request.request.body['ticker']).toBe('NVDA');
    });

    it('lets go of the old fill when the address moves on, so its late completion opens nothing', async () => {
      const pickers = world();
      pickers.coverage.holdNext = true;
      const opened = await open(`/markets/NVDA/chart?${WINDOW_QUERY}`, pickers);
      await screen.findByText(/NVDA is not in the lake/);
      pickers.coverage.holdNext = true;

      opened.context().switchSymbol('AMD');
      await screen.findByText(/AMD is not in the lake/);
      const [nvda, amd] = pickers.coverage.held;
      expect(nvda.cancelCalls).toBe(1);

      // NVDA's fill completes after the place moved to AMD.
      nvda.resolve(true);
      await new Promise((resolve) => setTimeout(resolve));
      opened.harness.fixture.detectChanges();
      expect(screen.getByText(/AMD is not in the lake/)).toBeTruthy();
      await expectNoChartRequest(opened);

      amd.resolve(true);
      const [request] = await chartRequests(opened.http, 1);
      expect(request.request.body['ticker']).toBe('AMD');
    });

    it('shows why a fill failed, retries the same fill, and goes back to Markets', async () => {
      const pickers = world();
      pickers.coverage.ensureResults.push(false);
      await open(`/markets/NVDA/chart?${WINDOW_QUERY}`, pickers);

      expect(await screen.findByText(/NVDA could not be filled/)).toBeTruthy();
      expect(screen.getByText(/Backfill Failed: The backfill job failed\./)).toBeTruthy();
      expect(screen.getByRole('link', { name: 'Back to Markets' })).toBeTruthy();

      await userEvent.click(screen.getByRole('button', { name: 'Retry the fill' }));
      expect(pickers.coverage.ensureCalls.map((call) => call.symbol)).toEqual(['NVDA', 'NVDA']);
    });
  });
});
