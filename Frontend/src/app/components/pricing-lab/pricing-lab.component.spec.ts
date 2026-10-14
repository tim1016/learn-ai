import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { vi } from 'vitest';
import { PricingLabComponent } from './pricing-lab.component';
import { etIsoDate } from '../../shared/date/et-midnight';
import { optionExpiryAnchorMs } from '../../shared/option-contract/option-expiry-date';
import { fakePickerWorld } from '../../shared/symbol-picker/testing/fake-picker-world';
import { PricingCompareResult, SnapshotContractResult } from '../../graphql/types';
import { bsPrice } from '../../utils/black-scholes';

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
    LineSeries: 'LineSeries',
    LineStyle: { Solid: 0, Dotted: 1, Dashed: 2, LargeDashed: 3, SparseDotted: 4 },
    CrosshairMode: { Normal: 0, Magnet: 1 },
    TickMarkType: { Year: 0, Month: 1, DayOfMonth: 2, Time: 3, TimeWithSeconds: 4 },
  };
});

const GRAPHQL_URL = 'http://localhost:5000/graphql';

function expectGraphQL(httpMock: HttpTestingController, needle: string) {
  const matches = httpMock.match(r =>
    r.url === GRAPHQL_URL && typeof (r.body as { query?: string } | null)?.query === 'string'
    && (r.body as { query: string }).query.includes(needle));
  if (matches.length !== 1) {
    throw new Error(`Expected exactly one GraphQL request matching "${needle}"; found ${matches.length}`);
  }
  return matches[0];
}

function buildContract(overrides: Partial<SnapshotContractResult> = {}): SnapshotContractResult {
  return {
    ticker: 'O:SPY260220C00590000',
    contractType: 'call',
    strikePrice: 590,
    expirationMs: 1_771_621_200_000,
    breakEvenPrice: 595,
    impliedVolatility: 0.20,
    openInterest: 1000,
    greeks: { delta: 0.5, gamma: 0.02, theta: -0.05, vega: 0.15 },
    day: { open: 5, high: 6, low: 4.5, close: 5.5, volume: 1000, vwap: null },
    lastTrade: null,
    lastQuote: null,
    ...overrides,
  };
}

describe('PricingLabComponent', () => {
  let component: PricingLabComponent;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      imports: [PricingLabComponent],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        // The ticker picker must not issue real catalog or jobs reads.
        ...fakePickerWorld().providers,
      ],
    });
    const fixture = TestBed.createComponent(PricingLabComponent);
    component = fixture.componentInstance;
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.match(() => true).forEach(req => {
      if (!req.cancelled) req.flush({ data: {} });
    });
    httpMock.verify();
  });

  // ── PL-A: Data-fetch prelude ───────────────────────────────────
  describe('PL-A: ticker → expirations → chain prelude', () => {
    it('selects nearest future expiration and triggers a chain fetch', async () => {
      component.ticker.set('SPY');
      const promise = component.fetchExpirations();

      const expReq = expectGraphQL(httpMock, 'getOptionsExpirations');
      const future = optionExpiryAnchorMs(etIsoDate(Date.now() + 30 * 86400000));
      expReq.flush({
        data: {
          getOptionsExpirations: {
            success: true,
            expirations: [future],
            count: 1,
            error: null,
          },
        },
      });

      // Yield a full event-loop turn so the awaited fetchChain() can issue its request.
      await new Promise(resolve => setTimeout(resolve, 0));

      const chainReq = expectGraphQL(httpMock, 'getOptionsChainSnapshot');
      chainReq.flush({
        data: {
          getOptionsChainSnapshot: {
            success: true,
            underlying: { ticker: 'SPY', price: 590, change: 1, changePercent: 0.17 },
            contracts: [buildContract()],
            count: 1,
            riskFreeRate: 0.045,
            dividendYield: 0.012,
            rateSource: 'fred',
            dividendSource: 'polygon',
            error: null,
          },
        },
      });

      await promise;

      expect(component.availableExpirations()).toEqual([future]);
      expect(component.selectedExpiration()).toBe(future);
      expect(component.underlying()?.price).toBe(590);
      expect(component.allContracts().length).toBe(1);
      expect(component.riskFreeRate()).toBe(0.045);  // auto-populated
      expect(component.expirationsLoading()).toBe(false);
    });

    it('takes a snapshot rate of exactly 0 as the rate', async () => {
      const promise = component.fetchChain('SPY', 4_070_984_400_000);
      expectGraphQL(httpMock, 'getOptionsChainSnapshot').flush({
        data: {
          getOptionsChainSnapshot: {
            success: true,
            underlying: { ticker: 'SPY', price: 590, change: 0, changePercent: 0 },
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
      await promise;

      expect(component.riskFreeRate()).toBe(0);
    });

    it('clears selected contract and prior server result when fetchChain runs again', async () => {
      // Seed prior state
      component.selectedContract.set(buildContract());
      component.serverResult.set({
        success: true, strike: 0, optionType: 'call', expirationMs: 0,
        timeToExpiryYears: 0, riskFreeRate: null, models: [], error: null,
      } satisfies PricingCompareResult);

      const promise = component.fetchChain('AAPL', 4_070_984_400_000);

      // Synchronous reset
      expect(component.selectedContract()).toBeNull();
      expect(component.serverResult()).toBeNull();
      expect(component.chainLoading()).toBe(true);

      const chainReq = expectGraphQL(httpMock, 'getOptionsChainSnapshot');
      chainReq.flush({
        data: {
          getOptionsChainSnapshot: {
            success: true,
            underlying: { ticker: 'AAPL', price: 230, change: 0, changePercent: 0 },
            contracts: [],
            count: 0,
            riskFreeRate: null,
            dividendYield: null,
            rateSource: null,
            dividendSource: null,
            error: null,
          },
        },
      });

      await promise;
      expect(component.chainLoading()).toBe(false);
    });

    // ── Failures and stale data are shown, not hidden (#3221) ──
    function chainReply(price: number, contracts: SnapshotContractResult[]) {
      return {
        data: {
          getOptionsChainSnapshot: {
            success: true,
            underlying: { ticker: 'SPY', price, change: 0, changePercent: 0 },
            contracts, count: contracts.length,
            riskFreeRate: null, dividendYield: null, rateSource: null, dividendSource: null, error: null,
          },
        },
      };
    }

    function pendingFor(needle: string, key: 'underlyingTicker' | 'expirationMs', value: string | number) {
      const found = httpMock.match(r => {
        const b = r.body as { query?: string; variables?: Record<string, unknown> } | null;
        return b?.query?.includes(needle) === true && b.variables?.[key] === value;
      });
      expect(found.length).toBe(1);
      return found[0];
    }

    it('says why the expirations could not be loaded', async () => {
      const promise = component.fetchExpirations();

      expectGraphQL(httpMock, 'getOptionsExpirations').flush({
        data: { getOptionsExpirations: { success: false, expirations: [], count: 0, error: 'Polygon rate limited' } },
      });
      await promise;

      expect(component.statusMessage()?.type).toBe('error');
      expect(component.statusMessage()?.text).toContain('Polygon rate limited');
      expect(component.expirationsLoading()).toBe(false);
    });

    it('says why the chain could not be loaded', async () => {
      const promise = component.fetchChain('SPY', 4_070_984_400_000);
      expectGraphQL(httpMock, 'getOptionsChainSnapshot').flush({
        data: {
          getOptionsChainSnapshot: {
            success: false, underlying: null, contracts: [], count: 0,
            riskFreeRate: null, dividendYield: null, rateSource: null, dividendSource: null, error: 'Snapshot unavailable',
          },
        },
      });
      await promise;

      expect(component.statusMessage()?.type).toBe('error');
      expect(component.statusMessage()?.text).toContain('Snapshot unavailable');
      expect(component.chainLoading()).toBe(false);
    });

    it('leaves no strikes from the previous expiry to pick when the chain request fails', async () => {
      component.allContracts.set([buildContract()]);

      const promise = component.fetchChain('SPY', 4_070_984_400_000);
      expectGraphQL(httpMock, 'getOptionsChainSnapshot')
        .error(new ProgressEvent('error'), { status: 500, statusText: 'Server Error' });
      await promise;

      expect(component.statusMessage()?.type).toBe('error');
      expect(component.allContracts()).toEqual([]);
    });

    it('keeps the later expiry\'s chain when the earlier expiry\'s reply lands last', async () => {
      const early = 4_070_984_400_000;
      const late = 4_073_576_400_000;
      const first = component.fetchChain('SPY', early);
      const second = component.fetchChain('SPY', late);

      pendingFor('getOptionsChainSnapshot', 'expirationMs', late)
        .flush(chainReply(600, [buildContract({ strikePrice: 600 })]));
      await second;
      pendingFor('getOptionsChainSnapshot', 'expirationMs', early)
        .flush(chainReply(500, [buildContract({ strikePrice: 500 })]));
      await first;

      expect(component.allContracts().map(c => c.strikePrice)).toEqual([600]);
      expect(component.underlying()?.price).toBe(600);
      expect(component.chainLoading()).toBe(false);
    });

    it('keeps the new ticker\'s expirations when the previous ticker\'s reply lands last', async () => {
      const aaplExpiry = optionExpiryAnchorMs(etIsoDate(Date.now() + 10 * 86400000));
      const msftExpiry = optionExpiryAnchorMs(etIsoDate(Date.now() + 20 * 86400000));
      component.ticker.set('AAPL');
      const first = component.fetchExpirations();
      component.ticker.set('MSFT');
      const second = component.fetchExpirations();

      pendingFor('getOptionsExpirations', 'underlyingTicker', 'MSFT').flush({
        data: { getOptionsExpirations: { success: true, expirations: [msftExpiry], count: 1, error: null } },
      });
      await new Promise(resolve => setTimeout(resolve, 0));
      pendingFor('getOptionsChainSnapshot', 'underlyingTicker', 'MSFT')
        .flush(chainReply(400, [buildContract({ strikePrice: 400 })]));
      await second;

      pendingFor('getOptionsExpirations', 'underlyingTicker', 'AAPL').flush({
        data: { getOptionsExpirations: { success: true, expirations: [aaplExpiry], count: 1, error: null } },
      });
      await first;

      expect(component.availableExpirations()).toEqual([msftExpiry]);
      expect(component.selectedExpiration()).toBe(msftExpiry);
      expect(component.allContracts().map(c => c.strikePrice)).toEqual([400]);
      expect(component.expirationsLoading()).toBe(false);
    });
  });

  // ── PL-B: Compare workflow ─────────────────────────────────────
  describe('PL-B: runComparison → pricingModelComparison resolver call', () => {
    function seedReadyState(): void {
      component.ticker.set('SPY');
      component.selectedExpiration.set(4_070_984_400_000);
      component.underlying.set({ ticker: 'SPY', price: 590 });
      component.allContracts.set([buildContract()]);
      component.selectedContract.set(buildContract());
    }

    it('forwards (spot, strike, vol, expiration, optionType, riskFreeRate, range, numPoints)', async () => {
      seedReadyState();
      component.riskFreeRate.set(0.045);
      component.spotRangePct.set(20);

      const promise = component.runComparison();
      expect(component.compareLoading()).toBe(true);

      const req = expectGraphQL(httpMock, 'pricingModelComparison');
      const vars = req.request.body.variables;
      expect(vars.spot).toBe(590);
      expect(vars.strike).toBe(590);
      expect(vars.volatility).toBe(0.20);
      expect(vars.expirationMs).toBe(4_070_984_400_000);
      expect(vars.optionType).toBe('call');
      expect(vars.riskFreeRate).toBe(0.045);
      expect(vars.spotMin).toBeCloseTo(590 * 0.8, 6);
      expect(vars.spotMax).toBeCloseTo(590 * 1.2, 6);
      expect(vars.numPoints).toBe(100);

      req.flush({
        data: {
          pricingModelComparison: {
            success: true,
            strike: 590, optionType: 'call', expirationMs: 4_070_984_400_000,
            timeToExpiryYears: 0.0822,
            models: [
              { model: 'python_bs', points: [{ spot: 590, price: 5.5, delta: 0.5, gamma: 0.02, theta: -0.05, vega: 0.15, rho: 0.05 }] },
              { model: 'quantlib_bs', points: [{ spot: 590, price: 5.499, delta: 0.5, gamma: 0.02, theta: -0.05, vega: 0.15, rho: 0.05 }] },
            ],
            error: null,
          },
        },
      });

      await promise;

      const serverResult = component.serverResult();
      expect(serverResult).not.toBeNull();
      if (!serverResult) throw new Error('Expected comparison result');
      expect(serverResult.models.length).toBe(2);
      expect(component.compareLoading()).toBe(false);
      expect(component.statusMessage()?.type).toBe('success');
    });

    it('sends no rate when the rate field is empty, so Python prices at its default', async () => {
      seedReadyState();
      component.riskFreeRate.set(null);

      const promise = component.runComparison();
      const req = expectGraphQL(httpMock, 'pricingModelComparison');
      expect(req.request.body.variables.riskFreeRate).toBeNull();

      req.flush({ data: { pricingModelComparison: { success: false, strike: 590, optionType: 'call', expirationMs: 4_070_984_400_000, timeToExpiryYears: 0, riskFreeRate: 0.043, models: [], error: 'stop' } } });
      await promise;
    });

    it('prices the in-browser Legacy BS overlay at the rate the server priced at, not at the field', () => {
      seedReadyState();
      component.spotRangePct.set(20);
      component.riskFreeRate.set(0.09);
      const serverRate = 0.043;
      const ttm = 0.25;
      const spotMin = 590 * 0.8;
      component.serverResult.set({
        success: true, strike: 590, optionType: 'call', expirationMs: 4_070_984_400_000,
        timeToExpiryYears: ttm, riskFreeRate: serverRate, error: null,
        models: [{
          model: 'python_bs',
          points: [{ spot: spotMin, price: bsPrice(spotMin, 590, serverRate, 0.20, ttm, 'call'), delta: 0, gamma: 0, theta: 0, vega: 0, rho: 0 }],
        }],
      });

      const pythonVsLegacy = component.summaryStats().find(row => row.key === 'python_bs');
      expect(pythonVsLegacy?.maxDiff).toBe(0);
    });

    it('records server-level errors in statusMessage and clears serverResult', async () => {
      seedReadyState();
      // Pre-seed a result so we can assert it gets cleared
      component.serverResult.set({ success: true, strike: 0, optionType: 'call', expirationMs: 0, timeToExpiryYears: 0, riskFreeRate: null, models: [], error: null });

      const promise = component.runComparison();
      const req = expectGraphQL(httpMock, 'pricingModelComparison');
      req.flush({
        data: {
          pricingModelComparison: {
            success: false,
            strike: 590, optionType: 'call', expirationMs: 4_070_984_400_000,
            timeToExpiryYears: 0,
            models: [],
            error: 'QuantLib not available',
          },
        },
      });
      await promise;

      expect(component.statusMessage()?.type).toBe('error');
      expect(component.statusMessage()?.text).toContain('QuantLib not available');
      expect(component.serverResult()).toBeNull();
      expect(component.compareLoading()).toBe(false);
    });

    it('catches network errors and surfaces them in statusMessage', async () => {
      seedReadyState();

      const promise = component.runComparison();
      const req = expectGraphQL(httpMock, 'pricingModelComparison');
      req.error(new ProgressEvent('error'), { status: 500, statusText: 'Server Error' });

      await promise;

      expect(component.statusMessage()?.type).toBe('error');
      expect(component.serverResult()).toBeNull();
      expect(component.compareLoading()).toBe(false);
    });
  });

  // ── PL-E: Edge cases ───────────────────────────────────────────
  describe('PL-E: edge cases', () => {
    it('runComparison sets a warn message when no contract is selected', async () => {
      component.underlying.set({ ticker: 'SPY', price: 590 });
      component.selectedContract.set(null);

      await component.runComparison();

      expect(component.statusMessage()?.type).toBe('warn');
      expect(component.statusMessage()?.text).toContain('contract');
      // No HTTP fired
      const matches = httpMock.match(r =>
        typeof (r.body as { query?: string } | null)?.query === 'string'
        && (r.body as { query: string }).query.includes('pricingModelComparison'));
      expect(matches.length).toBe(0);
    });

    it('runComparison sets a warn message when underlying price is missing', async () => {
      component.underlying.set(null);
      component.selectedContract.set(buildContract());

      await component.runComparison();

      expect(component.statusMessage()?.type).toBe('warn');
      const matches = httpMock.match(r =>
        typeof (r.body as { query?: string } | null)?.query === 'string'
        && (r.body as { query: string }).query.includes('pricingModelComparison'));
      expect(matches.length).toBe(0);
    });

    it('runComparison reports the missing fields when the contract is incomplete', async () => {
      component.underlying.set({ ticker: 'SPY', price: 590 });
      component.selectedExpiration.set(4_070_984_400_000);
      // Contract missing implied volatility — the component should call out which field is missing
      component.selectedContract.set(buildContract({ impliedVolatility: null }));

      await component.runComparison();

      expect(component.statusMessage()?.type).toBe('warn');
      expect(component.statusMessage()?.text).toContain('implied volatility');
      const matches = httpMock.match(r =>
        typeof (r.body as { query?: string } | null)?.query === 'string'
        && (r.body as { query: string }).query.includes('pricingModelComparison'));
      expect(matches.length).toBe(0);
    });
  });
});
