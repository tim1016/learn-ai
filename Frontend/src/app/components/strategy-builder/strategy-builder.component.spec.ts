import { TestBed, type ComponentFixture } from '@angular/core/testing';
import { screen, within } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { vi } from 'vitest';
import { StrategyBuilderComponent } from './strategy-builder.component';
import { etIsoDate } from '../../shared/date/et-midnight';
import { optionExpiryAnchorMs } from '../../shared/option-contract/option-expiry-date';

import { fakePickerWorld } from '../../shared/symbol-picker/testing/fake-picker-world';
import {
  SnapshotContractResult,
  StrategyAnalyzeResult,
} from '../../graphql/types';

vi.mock('lightweight-charts', () => {
  const mockTimeScale = { fitContent: vi.fn() };
  const createMockSeries = () => ({ setData: vi.fn(), applyOptions: vi.fn(), createPriceLine: vi.fn() });
  const createMockChart = () => ({
    addSeries: vi.fn().mockReturnValue(createMockSeries()),
    removeSeries: vi.fn(),
    timeScale: vi.fn().mockReturnValue(mockTimeScale),
    applyOptions: vi.fn(),
    remove: vi.fn(),
    subscribeCrosshairMove: vi.fn(),
    unsubscribeCrosshairMove: vi.fn(),
  });
  return {
    createChart: vi.fn().mockImplementation(() => createMockChart()),
    CandlestickSeries: 'CandlestickSeries',
    BaselineSeries: 'BaselineSeries',
    LineSeries: 'LineSeries',
    HistogramSeries: 'HistogramSeries',
    LineStyle: { Solid: 0, Dotted: 1, Dashed: 2, LargeDashed: 3, SparseDotted: 4 },
    CrosshairMode: { Normal: 0, Magnet: 1 },
    TickMarkType: { Year: 0, Month: 1, DayOfMonth: 2, Time: 3, TimeWithSeconds: 4 },
  };
});

const GRAPHQL_URL = 'http://localhost:5000/graphql';

/**
 * Match a GraphQL request whose body's `query` string contains `needle`.
 * The component sends raw GraphQL POST bodies with shape
 *   { query: '...', variables: {...} }
 * so we filter by query-text for unambiguous matching when multiple
 * requests are in flight.
 */
function expectGraphQL(httpMock: HttpTestingController, needle: string) {
  const matches = httpMock.match(r =>
    r.url === GRAPHQL_URL && typeof (r.body as { query?: string } | null)?.query === 'string'
    && (r.body as { query: string }).query.includes(needle));
  if (matches.length !== 1) {
    throw new Error(`Expected exactly one GraphQL request matching "${needle}"; found ${matches.length}`);
  }
  return matches[0];
}

function expectGraphQLOptional(httpMock: HttpTestingController, needle: string) {
  const matches = httpMock.match(r =>
    r.url === GRAPHQL_URL && typeof (r.body as { query?: string } | null)?.query === 'string'
    && (r.body as { query: string }).query.includes(needle));
  return matches[0] ?? null;
}

function buildAnalysisResult(): StrategyAnalyzeResult {
  return {
    success: true,
    symbol: 'SPY',
    spotPrice: 100,
    strategyCost: 0,
    pop: 0,
    expectedValue: 0,
    maxProfit: 0,
    maxLoss: 0,
    breakevens: [],
    curve: [],
    greeks: { delta: 0, gamma: 0, theta: 0, vega: 0 },
    currentCurve: null,
    greekCurves: null,
    legDiagnostics: null,
    error: null,
  };
}

describe('StrategyBuilderComponent', () => {
  let component: StrategyBuilderComponent;
  let fixture: ComponentFixture<StrategyBuilderComponent>;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      imports: [StrategyBuilderComponent],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        // The ticker picker must not construct the real catalog/jobs chain.
        ...fakePickerWorld().providers,
      ],
    });
    fixture = TestBed.createComponent(StrategyBuilderComponent);
    component = fixture.componentInstance;
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    // Drain any incidental requests fired by change detection so each
    // test only asserts the requests it explicitly cares about.
    httpMock.match(() => true).forEach(req => {
      if (!req.cancelled) req.flush({ data: {} });
    });
    httpMock.verify();
  });

  it('omits a broker identity and Greeks when multiple contracts match a hypothetical leg', async () => {
    const expiry = optionExpiryAnchorMs('2027-01-15');
    const snapshot: SnapshotContractResult = {
      ticker: 'O:SPY270115C00590000', contractType: 'call', strikePrice: 590, expirationMs: expiry,
      breakEvenPrice: null, impliedVolatility: 0.2, openInterest: null, greeks: { delta: 0.7, gamma: null, theta: null, vega: null },
      day: null, lastTrade: null, lastQuote: { bid: 3.1, ask: 3.2, bidSize: null, askSize: null, midpoint: 3.15, timeframe: null },
    };
    component.selectedExpiration.set(expiry);
    component.allContracts.set([snapshot, { ...snapshot, ticker: 'O:SPY1270115C00590000' }]);
    component.legs.set([{ strike: 590, optionType: 'call', position: 'long', premium: 3.2, iv: 0.2, quantity: 1, enabled: true }]);
    fixture.detectChanges();

    const root: HTMLElement = fixture.nativeElement;
    const chip = root.querySelector<HTMLElement>('.leg-chip .option-chip');
    if (!chip) throw new Error('Expected the leg’s option chip');
    await userEvent.hover(chip);
    const details = within(await screen.findByRole('tooltip'));
    expect(details.queryByText(snapshot.ticker ?? '')).toBeNull();
    expect(details.queryByText('Delta')).toBeNull();
    expect(details.queryByText('Bid')).toBeNull();
  });

  it('classifies a leg with the option chain’s spot rather than a separate stock close', async () => {
    const expiry = optionExpiryAnchorMs('2027-01-15');
    component.selectedExpiration.set(expiry);
    component.underlying.set({ ticker: 'SPY', price: 586, change: 0, changePercent: 0 });
    component.stockSnapshot.set({
      ticker: 'SPY', day: { open: null, high: null, low: null, close: 584, volume: null, vwap: null },
      prevDay: null, min: null, todaysChange: null, todaysChangePercent: null, updated: null,
    });
    component.allContracts.set([{
      ticker: 'O:SPY270115C00585000', contractType: 'call', strikePrice: 585, expirationMs: expiry,
      breakEvenPrice: null, impliedVolatility: 0.2, openInterest: null, greeks: null,
      day: null, lastTrade: null, lastQuote: null,
    }]);
    component.legs.set([{ strike: 585, optionType: 'call', position: 'long', premium: 3.2, iv: 0.2, quantity: 1, enabled: true }]);
    fixture.detectChanges();

    const root: HTMLElement = fixture.nativeElement;
    expect(root.querySelector('.leg-chip .option-chip__status')?.textContent).toBe('ITM');
  });

  // ── SB-A: Data-fetch prelude ───────────────────────────────────
  describe('SB-A: ticker → expirations → chain prelude', () => {
    it('populates expirations and selects the nearest one on fetchExpirations()', async () => {
      component.ticker.set('SPY');

      const promise = component.fetchExpirations();

      // fetchExpirations issues two parallel requests: getOptionsExpirations + getStockSnapshot
      const expReq = expectGraphQL(httpMock, 'getOptionsExpirations');
      const snapReq = expectGraphQLOptional(httpMock, 'getStockSnapshot');

      const futureDate = optionExpiryAnchorMs(etIsoDate(Date.now() + 30 * 86400000));
      expReq.flush({
        data: {
          getOptionsExpirations: {
            success: true,
            expirations: [futureDate],
            count: 1,
            error: null,
          },
        },
      });
      snapReq?.flush({
        data: { getStockSnapshot: { success: true, snapshot: null, error: null } },
      });

      // Yield a full event-loop turn so Promise.all resolves and the
      // awaited fetchChainSnapshot() can issue its request.
      await new Promise(resolve => setTimeout(resolve, 0));

      // The selection of the nearest expiration triggers a chain-snapshot fetch.
      const chainReq = expectGraphQL(httpMock, 'getOptionsChainSnapshot');
      chainReq.flush({
        data: {
          getOptionsChainSnapshot: {
            success: true,
            underlying: { ticker: 'SPY', price: 590, change: 1, changePercent: 0.17 },
            contracts: [],
            count: 0,
            riskFreeRate: 0.05,
            dividendYield: 0.012,
            rateSource: 'fred',
            dividendSource: 'polygon',
            error: null,
          },
        },
      });

      await promise;

      expect(component.availableExpirations()).toEqual([futureDate]);
      expect(component.selectedExpiration()).toBe(futureDate);
      expect(component.underlying()?.price).toBe(590);
      expect(component.riskFreeRate()).toBe(0.05);  // auto-populated from snapshot
      expect(component.expirationsLoading()).toBe(false);
    });

    it('clears prior chain state when the user kicks off a new fetch', async () => {
      // Seed prior state
      component.availableExpirations.set([1_767_301_200_000]);
      component.selectedExpiration.set(1_767_301_200_000);
      component.legs.set([{ strike: 100, optionType: 'call', position: 'long', premium: 5, iv: 0.3, quantity: 1, enabled: true }]);
      component.analysisResult.set(buildAnalysisResult());

      component.ticker.set('AAPL');
      const promise = component.fetchExpirations();

      // Synchronous reset happens before HTTP resolves
      expect(component.availableExpirations()).toEqual([]);
      expect(component.selectedExpiration()).toBeNull();
      expect(component.legs()).toEqual([]);
      expect(component.analysisResult()).toBeNull();
      expect(component.expirationsLoading()).toBe(true);

      // Drain to keep afterEach clean
      httpMock.match(() => true).forEach(r => {
        const body = r.request.body as { query?: string } | null;
        if (body?.query?.includes('getOptionsExpirations')) {
          r.flush({ data: { getOptionsExpirations: { success: true, expirations: [], count: 0, error: null } } });
        } else {
          r.flush({ data: {} });
        }
      });

      await promise;
    });
  });

  // ── Failures and stale data are shown, not hidden (#3221) ──────
  describe('failed and superseded fetches', () => {
    function expirationsReply(expirations: number[]) {
      return { data: { getOptionsExpirations: { success: true, expirations, count: expirations.length, error: null } } };
    }

    function snapshotReply(price: number, contracts: SnapshotContractResult[]) {
      return {
        data: {
          getOptionsChainSnapshot: {
            success: true,
            underlying: { ticker: 'SPY', price, change: 0, changePercent: 0 },
            contracts, count: contracts.length,
            riskFreeRate: 0.04, dividendYield: null, rateSource: 'FRED', dividendSource: null, error: null,
          },
        },
      };
    }

    function contract(strike: number): SnapshotContractResult {
      return {
        ticker: `O:SPY${strike}C`, contractType: 'call', strikePrice: strike, expirationMs: 4_070_984_400_000,
        breakEvenPrice: null, impliedVolatility: 0.2, openInterest: 10, greeks: null, day: null,
        lastTrade: null, lastQuote: { bid: 4.9, ask: 5.1, bidSize: 1, askSize: 1, midpoint: 5, timeframe: null },
      };
    }

    function flushByTicker(needle: string, ticker: string, body: object): void {
      const req = httpMock.match(r => {
        const b = r.body as { query?: string; variables?: { underlyingTicker?: string; ticker?: string } } | null;
        return b?.query?.includes(needle) === true
          && (b.variables?.underlyingTicker ?? b.variables?.ticker) === ticker;
      });
      expect(req.length).toBe(1);
      req[0].flush(body);
    }

    it('shows the vendor failure when expirations cannot be loaded', async () => {
      component.ticker.set('SPY');
      const promise = component.fetchExpirations();

      expectGraphQL(httpMock, 'getOptionsExpirations').flush({
        data: { getOptionsExpirations: { success: false, expirations: [], count: 0, error: 'Polygon rate limited' } },
      });
      expectGraphQLOptional(httpMock, 'getStockSnapshot')?.flush({ data: { getStockSnapshot: { success: false, snapshot: null, error: 'x' } } });
      await promise;

      expect(component.error()).toContain('Polygon rate limited');
      expect(component.availableExpirations()).toEqual([]);
      expect(component.expirationsLoading()).toBe(false);
    });

    it('keeps the new ticker\'s expirations when the previous ticker\'s reply lands last', async () => {
      const aaplExpiry = optionExpiryAnchorMs(etIsoDate(Date.now() + 10 * 86400000));
      const msftExpiry = optionExpiryAnchorMs(etIsoDate(Date.now() + 20 * 86400000));
      component.ticker.set('AAPL');
      const first = component.fetchExpirations();
      component.ticker.set('MSFT');
      const second = component.fetchExpirations();

      flushByTicker('getOptionsExpirations', 'MSFT', expirationsReply([msftExpiry]));
      flushByTicker('getStockSnapshot', 'MSFT', { data: { getStockSnapshot: { success: false, snapshot: null, error: null } } });
      await new Promise(resolve => setTimeout(resolve, 0));
      flushByTicker('getOptionsChainSnapshot', 'MSFT', snapshotReply(400, [contract(400)]));
      await second;

      flushByTicker('getOptionsExpirations', 'AAPL', expirationsReply([aaplExpiry]));
      flushByTicker('getStockSnapshot', 'AAPL', { data: { getStockSnapshot: { success: false, snapshot: null, error: null } } });
      await first;

      expect(component.availableExpirations()).toEqual([msftExpiry]);
      expect(component.selectedExpiration()).toBe(msftExpiry);
      expect(component.allContracts().map(c => c.strikePrice)).toEqual([400]);
      expect(component.expirationsLoading()).toBe(false);
    });

    it('drops the previous expiry\'s chain on selection, so a failed fetch leaves nothing to build legs from', async () => {
      component.ticker.set('SPY');
      component.allContracts.set([contract(100)]);
      component.legs.set([{ strike: 100, optionType: 'call', position: 'long', premium: 5, iv: 0.2, quantity: 1, enabled: true }]);

      const selecting = component.onExpirationSelected(4_078_000_000_000);

      expect(component.allContracts()).toEqual([]);
      expect(component.legs()).toEqual([]);
      expectGraphQL(httpMock, 'getOptionsChainSnapshot').flush({
        data: {
          getOptionsChainSnapshot: {
            success: false, underlying: null, contracts: [], count: 0,
            riskFreeRate: null, dividendYield: null, rateSource: null, dividendSource: null, error: 'Snapshot unavailable',
          },
        },
      });
      await selecting;

      expect(component.error()).toBe('Snapshot unavailable');
      expect(component.allContracts()).toEqual([]);
    });

    describe('the 8-leg cap', () => {
      function fullStrategy(): void {
        component.legs.set(Array.from({ length: 8 }, (_, i) => ({
          strike: 90 + i, optionType: 'call' as const, position: 'long' as const,
          premium: 5, iv: 0.2, quantity: 1, enabled: true,
        })));
      }

      it('says so when a ninth leg is refused', () => {
        fullStrategy();

        component.addLegFromChain(contract(200), 'call', 'long');

        expect(component.legs().length).toBe(8);
        expect(component.error()).toContain('at most 8 legs');
      });

      it('still adds a lot to a leg that is already in the strategy', () => {
        fullStrategy();

        component.addLegFromChain(contract(90), 'call', 'long');

        expect(component.legs().length).toBe(8);
        expect(component.legs()[0].quantity).toBe(2);
        expect(component.error()).toBeNull();
      });
    });
  });

  // ── SB-C: Analyze workflow ─────────────────────────────────────
  describe('SB-C: analyzeStrategy → resolver call → result rendering', () => {
    function seedReadyState(): void {
      component.ticker.set('SPY');
      component.selectedExpiration.set(4_070_984_400_000);
      component.underlying.set({ ticker: 'SPY', price: 100, change: 0, changePercent: 0 });
      component.legs.set([
        { strike: 100, optionType: 'call', position: 'long', premium: 5, iv: 0.3, quantity: 1, enabled: true },
        { strike: 105, optionType: 'call', position: 'short', premium: 3, iv: 0.28, quantity: 1, enabled: true },
      ]);
    }

    it('populates analysisResult from a successful analyzeOptionsStrategy response', async () => {
      seedReadyState();

      const promise = component.analyzeStrategy();
      expect(component.analyzing()).toBe(true);

      const req = expectGraphQL(httpMock, 'analyzeOptionsStrategy');
      req.flush({
        data: {
          analyzeOptionsStrategy: {
            success: true,
            symbol: 'SPY',
            spotPrice: 100,
            strategyCost: 2,
            pop: 0.45,
            expectedValue: 0.5,
            maxProfit: 3,
            maxLoss: -2,
            breakevens: [102],
            curve: [
              { price: 95, pnl: -2 },
              { price: 100, pnl: -2 },
              { price: 105, pnl: 3 },
            ],
            greeks: { delta: 0.05, gamma: 0.01, theta: -0.02, vega: 0.05 },
            currentCurve: null,
            greekCurves: null,
            legDiagnostics: null,
            error: null,
          },
        },
      });

      await promise;

      const result = component.analysisResult();
      expect(result).not.toBeNull();
      if (!result) throw new Error('Expected an analysis result');
      expect(result.success).toBe(true);
      expect(result.pop).toBe(0.45);
      expect(result.maxProfit).toBe(3);
      expect(result.maxLoss).toBe(-2);
      expect(result.breakevens).toEqual([102]);
      expect(result.curve.length).toBe(3);
      expect(component.analyzing()).toBe(false);
      expect(component.error()).toBeNull();
    });

    it('forwards the enabled legs payload to the resolver', async () => {
      seedReadyState();
      // Disable one leg; only enabled ones should be sent.
      component.legs.update(legs => [
        ...legs,
        { strike: 110, optionType: 'put', position: 'long', premium: 4, iv: 0.32, quantity: 1, enabled: false },
      ]);

      const promise = component.analyzeStrategy();

      const req = expectGraphQL(httpMock, 'analyzeOptionsStrategy');
      const requestBody = req.request.body as {
        variables: { legs: { optionType: string }[] };
      };
      const sentLegs = requestBody.variables.legs;
      expect(sentLegs.length).toBe(2);
      expect(sentLegs.every((leg) => leg.optionType === 'call')).toBe(true);

      req.flush({ data: { analyzeOptionsStrategy: { success: true, symbol: 'SPY', spotPrice: 100, strategyCost: 0, pop: 0, expectedValue: 0, maxProfit: 0, maxLoss: 0, breakevens: [], curve: [], greeks: { delta: 0, gamma: 0, theta: 0, vega: 0 }, currentCurve: null, greekCurves: null, legDiagnostics: null, error: null } } });

      await promise;
    });

    it('propagates resolver-level errors to the error signal', async () => {
      seedReadyState();

      const promise = component.analyzeStrategy();
      const req = expectGraphQL(httpMock, 'analyzeOptionsStrategy');

      req.flush({
        data: {
          analyzeOptionsStrategy: {
            success: false,
            symbol: 'SPY',
            spotPrice: 0, strategyCost: 0, pop: 0, expectedValue: 0,
            maxProfit: 0, maxLoss: 0,
            breakevens: [], curve: [],
            greeks: { delta: 0, gamma: 0, theta: 0, vega: 0 },
            currentCurve: null, greekCurves: null, legDiagnostics: null,
            error: 'Python service unavailable',
          },
        },
      });

      await promise;

      expect(component.error()).toBe('Python service unavailable');
      expect(component.analysisResult()).toBeNull();
      expect(component.analyzing()).toBe(false);
    });

    it('catches network errors and records them in the error signal', async () => {
      seedReadyState();

      const promise = component.analyzeStrategy();
      const req = expectGraphQL(httpMock, 'analyzeOptionsStrategy');
      req.error(new ProgressEvent('error'), { status: 500, statusText: 'Server Error' });

      await promise;

      expect(component.error()).toBeTruthy();
      expect(component.analyzing()).toBe(false);
      expect(component.analysisResult()).toBeNull();
    });
  });

  // ── SB-G: Edge cases ────────────────────────────────────────────
  describe('SB-G: edge cases', () => {
    it('analyzeStrategy is a no-op when there is no selected expiration', async () => {
      component.legs.set([
        { strike: 100, optionType: 'call', position: 'long', premium: 5, iv: 0.3, quantity: 1, enabled: true },
      ]);
      component.underlying.set({ ticker: 'SPY', price: 100, change: 0, changePercent: 0 });
      component.selectedExpiration.set(null);

      await component.analyzeStrategy();

      // No HTTP request should have fired
      const matches = httpMock.match(r =>
        typeof (r.body as { query?: string } | null)?.query === 'string'
        && (r.body as { query: string }).query.includes('analyzeOptionsStrategy'));
      expect(matches.length).toBe(0);
      expect(component.analyzing()).toBe(false);
    });

    it('analyzeStrategy is a no-op when the spot price is zero', async () => {
      component.legs.set([
        { strike: 100, optionType: 'call', position: 'long', premium: 5, iv: 0.3, quantity: 1, enabled: true },
      ]);
      component.selectedExpiration.set(4_070_984_400_000);
      // No underlying, no stockSnapshot → spotPrice() === 0

      await component.analyzeStrategy();

      const matches = httpMock.match(r =>
        typeof (r.body as { query?: string } | null)?.query === 'string'
        && (r.body as { query: string }).query.includes('analyzeOptionsStrategy'));
      expect(matches.length).toBe(0);
    });

    it.each<[boolean, string, ReturnType<StrategyBuilderComponent['legs']>]>([
      [false, 'no legs are enabled', []],
      [
        false,
        'a leg has invalid inputs (negative premium)',
        [{ strike: 100, optionType: 'call', position: 'long', premium: -1, iv: 0.3, quantity: 1, enabled: true }],
      ],
      [
        true,
        'an enabled leg has valid inputs and an expiration is set',
        [{ strike: 100, optionType: 'call', position: 'long', premium: 5, iv: 0.3, quantity: 1, enabled: true }],
      ],
    ])('canAnalyze is %s when %s', (want, _case, legs) => {
      component.selectedExpiration.set(4_070_984_400_000);
      component.legs.set(legs);
      expect(component.canAnalyze()).toBe(want);
    });
  });

  // ── #3186: extremes and breakevens come from the whole payoff ──
  describe('payoff extremes and breakevens are read off the whole payoff', () => {
    it('shows a short put its whole loss, at S = 0, not the loss inside the chart window', () => {
      component.legs.set([
        { strike: 590, optionType: 'put', position: 'short', premium: 5, iv: 0.3, quantity: 1, enabled: true },
      ]);

      // (5 − 590) × 100; the ±5 % window shows only −$2,450.
      expect(component.liveMaxLossDisplay()).toBe('−$58,500.00');
      expect(component.liveMaxProfitDisplay()).toBe('$500.00');
      expect(component.liveBreakevens()).toEqual([585]);
    });

    it('shows a long call unlimited profit and a loss of its premium', () => {
      component.legs.set([
        { strike: 100, optionType: 'call', position: 'long', premium: 5, iv: 0.3, quantity: 1, enabled: true },
      ]);

      expect(component.liveMaxProfitDisplay()).toBe('∞');
      expect(component.liveMaxLossDisplay()).toBe('−$500.00');
    });

    it('shows nothing while no leg is enabled', () => {
      component.legs.set([
        { strike: 590, optionType: 'put', position: 'short', premium: 5, iv: 0.3, quantity: 1, enabled: false },
      ]);

      expect(component.liveMaxLoss()).toBeNull();
      expect(component.liveBreakevens()).toEqual([]);
    });
  });

  // ── UX-Q2: chain density toggle ────────────────────────────────
  describe('UX-Q2: chain density toggle', () => {
    beforeEach(() => {
      // Reset persistence so each test starts from the documented default.
      localStorage.removeItem('sb.chainDensity');
    });

    it("defaults chainDensity to 'quick' when no preference is stored", () => {
      // The component instance constructed by beforeEach already read
      // localStorage at signal-init time; rebuild to pick up the cleared value.
      TestBed.resetTestingModule();
      TestBed.configureTestingModule({
        imports: [StrategyBuilderComponent],
        providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        // The ticker picker must not construct the real catalog/jobs chain.
        ...fakePickerWorld().providers,
      ],
      });
      fixture = TestBed.createComponent(StrategyBuilderComponent);
      expect(fixture.componentInstance.chainDensity()).toBe('quick');
    });

    it("toggleChainDensity flips quick ↔ greeks and persists the choice", () => {
      expect(component.chainDensity()).toBe('quick');

      component.toggleChainDensity();
      expect(component.chainDensity()).toBe('greeks');
      expect(localStorage.getItem('sb.chainDensity')).toBe('greeks');

      component.toggleChainDensity();
      expect(component.chainDensity()).toBe('quick');
      expect(localStorage.getItem('sb.chainDensity')).toBe('quick');
    });

    it('opens on the default and still toggles when the browser blocks storage', () => {
      const blocked = () => { throw new DOMException('Storage is blocked.', 'SecurityError'); };
      vi.stubGlobal('localStorage', { getItem: blocked, setItem: blocked });
      try {
        const page = TestBed.createComponent(StrategyBuilderComponent).componentInstance;
        expect(page.chainDensity()).toBe('quick');

        page.toggleChainDensity();
        expect(page.chainDensity()).toBe('greeks');
      } finally {
        vi.unstubAllGlobals();
      }
    });
  });

  // ── UX-Q1 / R0b: drill-down icon-per-side trigger ────────────────
  describe('UX-Q1: drill-down history drawer', () => {
    function buildContract(side: 'call' | 'put'): SnapshotContractResult {
      return {
        ticker: `O:SPY260220${side === 'call' ? 'C' : 'P'}00590000`,
        contractType: side,
        strikePrice: 590,
        expirationMs: 1_771_621_200_000,
        breakEvenPrice: 595,
        impliedVolatility: 0.20,
        openInterest: 1000,
        greeks: { delta: 0.5, gamma: 0.02, theta: -0.05, vega: 0.15 },
        day: { open: 5, high: 6, low: 4.5, close: 5.5, volume: 1000, vwap: null },
        lastTrade: null,
        lastQuote: null,
      };
    }

    it('openContractHistory populates state and fires the aggregates fetch', async () => {
      const contract = buildContract('call');

      const promise = component.openContractHistory(contract, 'call');

      // Synchronous state set before the HTTP resolves
      expect(component.historyDrawerOpen()).toBe(true);
      expect(component.historyLoading()).toBe(true);
      expect(component.selectedHistoryContract()?.ticker).toBe('O:SPY260220C00590000');
      expect(component.selectedHistoryContract()?.contractType).toBe('call');

      const reqs = httpMock.match(r =>
        r.url === 'http://localhost:5000/graphql'
        && typeof (r.body as { query?: string } | null)?.query === 'string'
        && (r.body as { query: string }).query.includes('getOrFetchStockAggregates'));
      expect(reqs.length).toBe(1);
      reqs[0].flush({
        data: {
          getOrFetchStockAggregates: {
            ticker: 'O:SPY260220C00590000',
            aggregates: [
              { open: 5, high: 6, low: 4, close: 5.5, volume: 1000, timestamp: '2026-02-19T00:00:00Z' },
            ],
          },
        },
      });

      await promise;

      expect(component.historyLoading()).toBe(false);
      expect(component.historyAggregates().length).toBe(1);
      expect(component.historyError()).toBeNull();
    });

    it('openContractHistory is a no-op for a null contract or one without a ticker', async () => {
      await component.openContractHistory(null, 'call');
      expect(component.historyDrawerOpen()).toBe(false);
      expect(component.selectedHistoryContract()).toBeNull();

      await component.openContractHistory({ ...buildContract('put'), ticker: null }, 'put');
      expect(component.historyDrawerOpen()).toBe(false);
    });

    it('closeHistoryDrawer clears all drill-down state', () => {
      component.historyDrawerOpen.set(true);
      component.selectedHistoryContract.set({
        ticker: 'O:SPY260220C00590000', underlying: 'SPY',
        contractType: 'call', strikePrice: 590, expirationMs: 1_771_621_200_000,
        snapshot: buildContract('call'),
      });
      component.historyAggregates.set([{
        id: 1, tickerId: 1, open: 5, high: 6, low: 4, close: 5.5,
        volume: 1000, volumeWeightedAveragePrice: null, timestamp: 1_771_465_600_000,
        timespan: 'day', multiplier: 1, transactionCount: null,
      }]);
      component.historyError.set('boom');

      component.closeHistoryDrawer();

      expect(component.historyDrawerOpen()).toBe(false);
      expect(component.selectedHistoryContract()).toBeNull();
      expect(component.historyAggregates()).toEqual([]);
      expect(component.historyError()).toBeNull();
    });
  });

  // ── Risk-free rate (#2764) ─────────────────────────────────────
  // The in-browser curves price at Python's rate from the chain snapshot and
  // never at a rate of their own.
  describe('risk-free rate', () => {
    function withPricedLeg(): void {
      const future = optionExpiryAnchorMs(etIsoDate(Date.now() + 30 * 86400000));
      component.selectedExpiration.set(future);
      component.underlying.set({ ticker: 'SPY', price: 100, change: 0, changePercent: 0 });
      component.legs.set([
        { strike: 100, optionType: 'call', position: 'long', premium: 5, iv: 0.3, quantity: 1, enabled: true },
      ]);
    }

    it('draws forward curves and live Greeks only once Python has supplied a rate', () => {
      withPricedLeg();

      expect(component.riskFreeRate()).toBeNull();
      expect(component.currentPnlCurve()).toEqual([]);
      expect(component.greekCurve()).toEqual([]);
      expect(component.liveGreeks()).toBeNull();

      component.riskFreeRate.set(0.043);

      expect(component.currentPnlCurve().length).toBeGreaterThan(0);
      expect(component.greekCurve().length).toBeGreaterThan(0);
      expect(component.liveGreeks()).not.toBeNull();
    });

    it('sends the snapshot rate to the server analysis, even a rate of 0', async () => {
      component.ticker.set('SPY');
      component.selectedExpiration.set(4_070_984_400_000);
      const chain = component.fetchChainSnapshot('SPY', 4_070_984_400_000);
      expectGraphQL(httpMock, 'getOptionsChainSnapshot').flush({
        data: {
          getOptionsChainSnapshot: {
            success: true,
            underlying: { ticker: 'SPY', price: 100, change: 0, changePercent: 0 },
            contracts: [],
            count: 0,
            riskFreeRate: 0,
            dividendYield: null,
            rateSource: 'FRED',
            dividendSource: null,
            error: null,
          },
        },
      });
      await chain;
      withPricedLeg();

      const analysis = component.analyzeStrategy();
      const req = expectGraphQL(httpMock, 'analyzeOptionsStrategy');
      expect(req.request.body.variables.riskFreeRate).toBe(0);

      req.flush({ data: { analyzeOptionsStrategy: buildAnalysisResult() } });
      await analysis;
    });

    // Each expiry prices at its own tenor's rate (#2789).
    it('never prices a new expiry at the previous expiry\'s rate', async () => {
      function flushSnapshot(rate: number): void {
        expectGraphQL(httpMock, 'getOptionsChainSnapshot').flush({
          data: {
            getOptionsChainSnapshot: {
              success: true,
              underlying: { ticker: 'SPY', price: 100, change: 0, changePercent: 0 },
              contracts: [],
              count: 0,
              riskFreeRate: rate,
              dividendYield: null,
              rateSource: 'FRED',
              dividendSource: null,
              error: null,
            },
          },
        });
      }
      const nearExpiry = optionExpiryAnchorMs(etIsoDate(Date.now() + 30 * 86400000));
      const farExpiry = optionExpiryAnchorMs(etIsoDate(Date.now() + 182 * 86400000));
      component.ticker.set('SPY');
      component.selectedExpiration.set(nearExpiry);
      const nearChain = component.fetchChainSnapshot('SPY', nearExpiry);
      flushSnapshot(0.0401);
      await nearChain;

      const farChain = component.onExpirationSelected(farExpiry);
      component.legs.set([
        { strike: 100, optionType: 'call', position: 'long', premium: 5, iv: 0.3, quantity: 1, enabled: true },
      ]);

      // Until the far expiry's snapshot lands there is no rate, so nothing is priced.
      expect(component.currentPnlCurve()).toEqual([]);
      expect(component.liveGreeks()).toBeNull();

      flushSnapshot(0.05);
      await farChain;
      expect(component.currentPnlCurve().length).toBeGreaterThan(0);

      const analysis = component.analyzeStrategy();
      const req = expectGraphQL(httpMock, 'analyzeOptionsStrategy');
      expect(req.request.body.variables.expirationMs).toBe(farExpiry);
      expect(req.request.body.variables.riskFreeRate).toBe(0.05);

      req.flush({ data: { analyzeOptionsStrategy: buildAnalysisResult() } });
      await analysis;
    });

    it('ignores a late snapshot reply for an expiry the user has left', async () => {
      function snapshotReply(rate: number) {
        return {
          data: {
            getOptionsChainSnapshot: {
              success: true,
              underlying: { ticker: 'SPY', price: 100, change: 0, changePercent: 0 },
              contracts: [],
              count: 0,
              riskFreeRate: rate,
              dividendYield: null,
              rateSource: 'FRED',
              dividendSource: null,
              error: null,
            },
          },
        };
      }
      const nearExpiry = optionExpiryAnchorMs(etIsoDate(Date.now() + 30 * 86400000));
      const farExpiry = optionExpiryAnchorMs(etIsoDate(Date.now() + 182 * 86400000));
      component.ticker.set('SPY');
      component.selectedExpiration.set(nearExpiry);
      const nearChain = component.fetchChainSnapshot('SPY', nearExpiry);
      const farChain = component.onExpirationSelected(farExpiry);

      const pending = httpMock.match(r =>
        r.url === GRAPHQL_URL && (r.body as { query: string }).query.includes('getOptionsChainSnapshot'));
      expect(pending.map(r => r.request.body.variables.expirationMs)).toEqual([nearExpiry, farExpiry]);
      pending[1].flush(snapshotReply(0.05));
      await farChain;
      pending[0].flush(snapshotReply(0.0401));
      await nearChain;

      expect(component.selectedExpiration()).toBe(farExpiry);
      expect(component.riskFreeRate()).toBe(0.05);
    });

    it('lets neither a late failure nor a late finish for a left expiry touch the current fetch', async () => {
      const nearExpiry = optionExpiryAnchorMs(etIsoDate(Date.now() + 30 * 86400000));
      const farExpiry = optionExpiryAnchorMs(etIsoDate(Date.now() + 182 * 86400000));
      component.ticker.set('SPY');
      component.selectedExpiration.set(nearExpiry);
      const nearChain = component.fetchChainSnapshot('SPY', nearExpiry);
      const farChain = component.onExpirationSelected(farExpiry);

      const pending = httpMock.match(r =>
        r.url === GRAPHQL_URL && (r.body as { query: string }).query.includes('getOptionsChainSnapshot'));
      pending[0].flush({ errors: [{ message: 'near expiry failed' }] });
      await nearChain;

      expect(component.error()).toBeNull();
      expect(component.chainLoading()).toBe(true);

      pending[1].flush({
        data: {
          getOptionsChainSnapshot: {
            success: true,
            underlying: { ticker: 'SPY', price: 100, change: 0, changePercent: 0 },
            contracts: [],
            count: 0,
            riskFreeRate: 0.05,
            dividendYield: null,
            rateSource: 'FRED',
            dividendSource: null,
            error: null,
          },
        },
      });
      await farChain;
      expect(component.chainLoading()).toBe(false);
      expect(component.riskFreeRate()).toBe(0.05);
    });
  });

  // ── UX: zero-TTM banner ─────────────────────────────────────────
  // The forward-looking curves (currentPnlCurve, greekCurve,
  // whatIfCurves) silently return [] when timeToExpiry() <= 0 because
  // BS Greeks are degenerate at T=0. This banner makes that state
  // legible instead of silent.
  describe('UX: noForwardCurves banner trigger', () => {
    function withEnabledLeg(): void {
      component.legs.set([
        { strike: 100, optionType: 'call', position: 'long', premium: 5, iv: 0.3, quantity: 1, enabled: true },
      ]);
    }

    it('is true when legs exist and the selected expiration has already passed', () => {
      withEnabledLeg();
      // Yesterday's 16:00 ET anchor is behind us at any hour of today.
      const yesterdayAnchor = optionExpiryAnchorMs(etIsoDate(Date.now() - 86400000));
      component.selectedExpiration.set(yesterdayAnchor);

      expect(component.timeToExpiry()).toBe(0);
      expect(component.noForwardCurves()).toBe(true);
    });

    it('is false when timeToExpiry > 0 (future expiration with enabled legs)', () => {
      withEnabledLeg();
      const future = optionExpiryAnchorMs(etIsoDate(Date.now() + 30 * 86400000));
      component.selectedExpiration.set(future);
      // Sanity: timeToExpiry should now be positive.
      expect(component.timeToExpiry()).toBeGreaterThan(0);
      expect(component.noForwardCurves()).toBe(false);
    });

    it.each<[string, ReturnType<StrategyBuilderComponent['legs']>]>([
      ['no legs are enabled (banner only fires once a leg exists)', []],
      [
        'leg is present but disabled',
        [{ strike: 100, optionType: 'call', position: 'long', premium: 5, iv: 0.3, quantity: 1, enabled: false }],
      ],
    ])('is false when %s', (_case, legs) => {
      const todayAnchor = optionExpiryAnchorMs(etIsoDate(Date.now()));
      component.selectedExpiration.set(todayAnchor);
      component.legs.set(legs);
      expect(component.noForwardCurves()).toBe(false);
    });
  });

  // ── #3221: the clock, the expiry pick and the unit of the figures ──
  describe('time left, the nearest expiry and the expected value', () => {
    afterEach(() => {
      vi.useRealTimers();
    });

    it('counts the time left down on a page left open, with no input touched', () => {
      const openedAt = Date.UTC(2026, 9, 14, 15, 0);
      vi.useFakeTimers();
      vi.setSystemTime(openedAt);
      const page = TestBed.createComponent(StrategyBuilderComponent).componentInstance;
      page.selectedExpiration.set(openedAt + 10 * 86_400_000);
      expect(page.daysToExpiry()).toBeCloseTo(10, 9);

      vi.advanceTimersByTime(6 * 3_600_000);

      expect(page.daysToExpiry()).toBeCloseTo(9.75, 6);
    });

    it('opens on the next expiry once today\'s has closed at 16:00 New York time', async () => {
      // 16:30 EDT on Wed 2026-10-14: today's expiry anchor, 16:00 ET, is behind us.
      const now = Date.UTC(2026, 9, 14, 20, 30);
      vi.useFakeTimers({ toFake: ['Date'] });
      vi.setSystemTime(now);
      const todaysExpiry = optionExpiryAnchorMs(etIsoDate(now));
      const nextExpiry = optionExpiryAnchorMs(etIsoDate(now + 2 * 86_400_000));
      component.ticker.set('SPY');

      const promise = component.fetchExpirations();
      expectGraphQL(httpMock, 'getOptionsExpirations').flush({
        data: {
          getOptionsExpirations: { success: true, expirations: [todaysExpiry, nextExpiry], count: 2, error: null },
        },
      });
      expectGraphQLOptional(httpMock, 'getStockSnapshot')?.flush({
        data: { getStockSnapshot: { success: true, snapshot: null, error: null } },
      });
      await new Promise(resolve => setTimeout(resolve, 0));
      expectGraphQL(httpMock, 'getOptionsChainSnapshot').flush({
        data: {
          getOptionsChainSnapshot: {
            success: true,
            underlying: { ticker: 'SPY', price: 590, change: 0, changePercent: 0 },
            contracts: [],
            count: 0,
            riskFreeRate: 0.05,
            dividendYield: 0,
            rateSource: 'fred',
            dividendSource: 'polygon',
            error: null,
          },
        },
      });
      await promise;

      expect(component.selectedExpiration()).toBe(nextExpiry);
    });

    it('shows the expected value per contract, as it shows max profit and max loss', () => {
      // The server's figures are per share; the cards beside one another are per contract.
      component.analysisResult.set({ ...buildAnalysisResult(), expectedValue: 0.5, maxProfit: 0.5 });

      expect(component.analysisExpectedValueDisplay()).toBe('$50.00');
      expect(component.analysisMaxProfitDisplay()).toBe('$50.00');
    });
  });

  // ── Auto-analyze edge cases (PR #86 review feedback) ────────────
  // The auto-analyze effect was shipped with two latent bugs that
  // Codex flagged on PR #86 after the merge. These regressions guard
  // the fixes: P1 — re-fire after in-flight completion if inputs
  // changed; P2 — cancel pending timer when state becomes invalid.
  describe('auto-analyze edge cases', () => {
    /** Wait the 300ms debounce window plus a small safety buffer. */
    const waitForDebounce = () => new Promise<void>(resolve => setTimeout(resolve, 350));

    /** Set the minimum state needed for canAnalyze() to be true. */
    function makeAnalyzable() {
      const future = optionExpiryAnchorMs(etIsoDate(Date.now() + 30 * 86400000));
      component.selectedExpiration.set(future);
      // Spot of 100 via stockSnapshot.day.close (spotPrice computed).
      component.stockSnapshot.set({
        ticker: 'SPY',
        day: { open: 100, high: 100, low: 100, close: 100, volume: 1, vwap: 100 },
        prevDay: null, min: null,
        todaysChange: 0, todaysChangePercent: 0, updated: Date.now(),
      });
      component.legs.set([
        { strike: 100, optionType: 'call', position: 'long', premium: 5, iv: 0.3, quantity: 1, enabled: true },
      ]);
    }

    /** Find every captured analyzeOptionsStrategy GraphQL request. */
    function findAnalyzeRequests() {
      return httpMock.match(r =>
        r.url === GRAPHQL_URL
        && typeof (r.body as { query?: string } | null)?.query === 'string'
        && (r.body as { query: string }).query.includes('analyzeOptionsStrategy'));
    }

    function flushAnalyzeOk(req: ReturnType<typeof findAnalyzeRequests>[number]) {
      req.flush({
        data: {
          analyzeOptionsStrategy: {
            success: true, pop: 0.5, expectedValue: 10,
            maxProfit: 100, maxLoss: -50, breakevens: [105], error: null,
          },
        },
      });
    }

    it('P1: re-analyzes after completion when inputs changed mid-flight', async () => {
      makeAnalyzable();
      await waitForDebounce();

      const firstReqs = findAnalyzeRequests();
      expect(firstReqs.length).toBe(1);

      // Edit while first analyze is in flight (analyzing()=true, so
      // canAnalyze() is false). Pre-fix, the effect early-returned and
      // the new leg was silently dropped.
      component.legs.update(legs => [...legs, {
        strike: 110, optionType: 'call', position: 'long', premium: 3, iv: 0.3, quantity: 1, enabled: true,
      }]);

      // Resolve first analyze; effect 2 should observe analyzing flip
      // false + inputsDirtyDuringAnalysis=true and schedule a follow-up.
      flushAnalyzeOk(firstReqs[0]);
      await waitForDebounce();

      const secondReqs = findAnalyzeRequests();
      expect(secondReqs.length).toBe(1);
      flushAnalyzeOk(secondReqs[0]);
    });

    it('P2: cancels pending debounce timer when legs are cleared mid-window', async () => {
      makeAnalyzable();
      // Wait less than the 300ms debounce — timer is still pending
      await new Promise<void>(resolve => setTimeout(resolve, 100));
      component.clearLegs();
      // Wait past the original debounce expiry
      await new Promise<void>(resolve => setTimeout(resolve, 300));

      // Pre-fix the stale timer would fire and call analyzeStrategy()
      // with empty legs. The fix cancels in scheduleAnalysisIfReady()
      // before the canAnalyze() guard.
      expect(findAnalyzeRequests().length).toBe(0);
    });
  });
});
