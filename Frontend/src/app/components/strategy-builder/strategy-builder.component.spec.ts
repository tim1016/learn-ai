import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { vi } from 'vitest';
import { StrategyBuilderComponent } from './strategy-builder.component';

vi.mock('lightweight-charts', () => {
  const mockTimeScale = { fitContent: vi.fn() };
  const createMockSeries = () => ({ setData: vi.fn(), applyOptions: vi.fn() });
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
    LineSeries: 'LineSeries',
    HistogramSeries: 'HistogramSeries',
    LineStyle: { Solid: 0, Dotted: 1, Dashed: 2, LargeDashed: 3, SparseDotted: 4 },
    CrosshairMode: { Normal: 0, Magnet: 1 },
  };
});

const GRAPHQL_URL = 'http://localhost:5000/graphql';

/**
 * Match a GraphQL request whose body's `query` string contains `needle`.
 * The component sends Apollo-style POST bodies with shape
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

describe('StrategyBuilderComponent', () => {
  let component: StrategyBuilderComponent;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      imports: [StrategyBuilderComponent],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    const fixture = TestBed.createComponent(StrategyBuilderComponent);
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

  describe('initialization', () => {
    it('creates the component', () => {
      expect(component).toBeTruthy();
    });

    it('defaults ticker to SPY', () => {
      expect(component.ticker()).toBe('SPY');
    });

    it('starts with no legs and no analysis result', () => {
      expect(component.legs().length).toBe(0);
      expect(component.analysisResult()).toBeNull();
    });

    it('defaults riskFreeRate to 0.043', () => {
      expect(component.riskFreeRate()).toBe(0.043);
    });
  });

  // ── SB-A: Data-fetch prelude ───────────────────────────────────
  describe('SB-A: ticker → expirations → chain prelude', () => {
    it('populates expirations and selects the nearest one on fetchExpirations()', async () => {
      component.ticker.set('SPY');

      const promise = component.fetchExpirations();

      // fetchExpirations issues two parallel requests: getOptionsExpirations + getStockSnapshot
      const expReq = expectGraphQL(httpMock, 'getOptionsExpirations');
      const snapReq = expectGraphQLOptional(httpMock, 'getStockSnapshot');

      const futureDate = new Date(Date.now() + 30 * 86400000).toISOString().slice(0, 10);
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
      component.availableExpirations.set(['2026-01-01']);
      component.selectedExpiration.set('2026-01-01');
      component.legs.set([{ strike: 100, optionType: 'call', position: 'long', premium: 5, iv: 0.3, quantity: 1, enabled: true }]);
      component.analysisResult.set({ success: true } as any);

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

  // ── SB-C: Analyze workflow ─────────────────────────────────────
  describe('SB-C: analyzeStrategy → resolver call → result rendering', () => {
    function seedReadyState(): void {
      component.ticker.set('SPY');
      component.selectedExpiration.set('2099-01-01');
      component.underlying.set({ ticker: 'SPY', price: 100, change: 0, changePercent: 0 } as any);
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
      expect(result!.success).toBe(true);
      expect(result!.pop).toBe(0.45);
      expect(result!.maxProfit).toBe(3);
      expect(result!.maxLoss).toBe(-2);
      expect(result!.breakevens).toEqual([102]);
      expect(result!.curve.length).toBe(3);
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
      const sentLegs = req.request.body.variables.legs;
      expect(sentLegs.length).toBe(2);
      expect(sentLegs.every((l: any) => l.optionType === 'call')).toBe(true);

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
      component.underlying.set({ ticker: 'SPY', price: 100, change: 0, changePercent: 0 } as any);
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
      component.selectedExpiration.set('2099-01-01');
      // No underlying, no stockSnapshot → spotPrice() === 0

      await component.analyzeStrategy();

      const matches = httpMock.match(r =>
        typeof (r.body as { query?: string } | null)?.query === 'string'
        && (r.body as { query: string }).query.includes('analyzeOptionsStrategy'));
      expect(matches.length).toBe(0);
    });

    it('canAnalyze is false when no legs are enabled', () => {
      component.selectedExpiration.set('2099-01-01');
      component.legs.set([]);
      expect(component.canAnalyze()).toBe(false);
    });

    it('canAnalyze is false when a leg has invalid inputs (negative premium)', () => {
      component.selectedExpiration.set('2099-01-01');
      component.legs.set([
        { strike: 100, optionType: 'call', position: 'long', premium: -1, iv: 0.3, quantity: 1, enabled: true },
      ]);
      expect(component.canAnalyze()).toBe(false);
    });

    it('canAnalyze is true when an enabled leg has valid inputs and an expiration is set', () => {
      component.selectedExpiration.set('2099-01-01');
      component.legs.set([
        { strike: 100, optionType: 'call', position: 'long', premium: 5, iv: 0.3, quantity: 1, enabled: true },
      ]);
      expect(component.canAnalyze()).toBe(true);
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
        providers: [provideHttpClient(), provideHttpClientTesting()],
      });
      const fixture = TestBed.createComponent(StrategyBuilderComponent);
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
  });

  // ── UX-Q1 / R0b: drill-down icon-per-side trigger ────────────────
  describe('UX-Q1: drill-down history drawer', () => {
    function buildContract(side: 'call' | 'put') {
      return {
        ticker: `O:SPY260220${side === 'call' ? 'C' : 'P'}00590000`,
        contractType: side,
        strikePrice: 590,
        expirationDate: '2026-02-20',
        breakEvenPrice: 595,
        impliedVolatility: 0.20,
        openInterest: 1000,
        greeks: { delta: 0.5, gamma: 0.02, theta: -0.05, vega: 0.15 },
        day: { open: 5, high: 6, low: 4.5, close: 5.5, volume: 1000 },
        lastTrade: null,
        lastQuote: null,
      } as any;
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
            summary: null,
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

      await component.openContractHistory({ ticker: null } as any, 'put');
      expect(component.historyDrawerOpen()).toBe(false);
    });

    it('closeHistoryDrawer clears all drill-down state', () => {
      component.historyDrawerOpen.set(true);
      component.selectedHistoryContract.set({
        ticker: 'O:SPY260220C00590000',
        contractType: 'call', strikePrice: 590, expirationDate: '2026-02-20',
        snapshot: buildContract('call'),
      });
      component.historyAggregates.set([
        { open: 5, high: 6, low: 4, close: 5.5, volume: 1000, timestamp: '2026-02-19T00:00:00Z' } as any,
      ]);
      component.historyError.set('boom');

      component.closeHistoryDrawer();

      expect(component.historyDrawerOpen()).toBe(false);
      expect(component.selectedHistoryContract()).toBeNull();
      expect(component.historyAggregates()).toEqual([]);
      expect(component.historyError()).toBeNull();
    });

    it('parsedHistoryTicker returns the expected display fields for a SPY call', () => {
      component.selectedHistoryContract.set({
        ticker: 'O:SPY260220C00590000',
        contractType: 'call', strikePrice: 590, expirationDate: '2026-02-20',
        snapshot: buildContract('call'),
      });

      const parsed = component.parsedHistoryTicker();
      expect(parsed?.underlying).toBe('SPY');
      expect(parsed?.expDate).toBe('Feb 20, 2026');
      expect(parsed?.type).toBe('Call');
      expect(parsed?.strike).toBe('$590.00');
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

    it('is true when legs exist and selectedExpiration evaluates to T <= 0', () => {
      withEnabledLeg();
      const todayIso = new Date().toISOString().slice(0, 10);
      component.selectedExpiration.set(todayIso);

      // timeToExpiry clamps to 0 once the 16:00 ET deadline passes;
      // for any same-day exp the value is at most a few hours, but
      // the banner should fire when the live computation returns 0
      // OR a negligibly small T. Verify by checking the computed
      // signal directly: if the legs exist and timeToExpiry() <= 0
      // we want the banner.
      const expectedBanner = component.timeToExpiry() <= 0;
      expect(component.noForwardCurves()).toBe(expectedBanner);
    });

    it('is false when no legs are enabled (banner only fires once a leg exists)', () => {
      const todayIso = new Date().toISOString().slice(0, 10);
      component.selectedExpiration.set(todayIso);
      component.legs.set([]);
      expect(component.noForwardCurves()).toBe(false);
    });

    it('is false when timeToExpiry > 0 (future expiration with enabled legs)', () => {
      withEnabledLeg();
      const future = new Date(Date.now() + 30 * 86400000).toISOString().slice(0, 10);
      component.selectedExpiration.set(future);
      // Sanity: timeToExpiry should now be positive.
      expect(component.timeToExpiry()).toBeGreaterThan(0);
      expect(component.noForwardCurves()).toBe(false);
    });

    it('is false when leg is present but disabled', () => {
      const todayIso = new Date().toISOString().slice(0, 10);
      component.selectedExpiration.set(todayIso);
      component.legs.set([
        { strike: 100, optionType: 'call', position: 'long', premium: 5, iv: 0.3, quantity: 1, enabled: false },
      ]);
      expect(component.noForwardCurves()).toBe(false);
    });
  });
});
