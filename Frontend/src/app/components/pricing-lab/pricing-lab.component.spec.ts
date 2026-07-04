/* eslint-disable @typescript-eslint/no-explicit-any, @typescript-eslint/no-non-null-assertion */
import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { vi } from 'vitest';
import { PricingLabComponent } from './pricing-lab.component';
import { SnapshotContractResult } from '../../graphql/types';

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
    expirationDate: '2026-02-20',
    breakEvenPrice: 595,
    impliedVolatility: 0.20,
    openInterest: 1000,
    greeks: { delta: 0.5, gamma: 0.02, theta: -0.05, vega: 0.15 } as any,
    day: { open: 5, high: 6, low: 4.5, close: 5.5, volume: 1000 } as any,
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
      providers: [provideHttpClient(), provideHttpClientTesting()],
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

  describe('initialization', () => {
    it('creates the component', () => {
      expect(component).toBeTruthy();
    });

    it('defaults ticker to SPY', () => {
      expect(component.ticker()).toBe('SPY');
    });

    it('starts with no expirations, no contract, no result', () => {
      expect(component.availableExpirations()).toEqual([]);
      expect(component.selectedContract()).toBeNull();
      expect(component.serverResult()).toBeNull();
    });

    it('defaults riskFreeRate to 0.05', () => {
      expect(component.riskFreeRate()).toBe(0.05);
    });
  });

  // ── PL-A: Data-fetch prelude ───────────────────────────────────
  describe('PL-A: ticker → expirations → chain prelude', () => {
    it('selects nearest future expiration and triggers a chain fetch', async () => {
      component.ticker.set('SPY');
      const promise = component.fetchExpirations();

      const expReq = expectGraphQL(httpMock, 'getOptionsExpirations');
      const future = new Date(Date.now() + 30 * 86400000).toISOString().slice(0, 10);
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

    it('clears selected contract and prior server result when fetchChain runs again', async () => {
      // Seed prior state
      component.selectedContract.set(buildContract());
      component.serverResult.set({ success: true } as any);

      const promise = component.fetchChain('AAPL', '2099-01-01');

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
  });

  // ── PL-B: Compare workflow ─────────────────────────────────────
  describe('PL-B: runComparison → pricingModelComparison resolver call', () => {
    function seedReadyState(): void {
      component.ticker.set('SPY');
      component.selectedExpiration.set('2099-01-01');
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
      expect(vars.expirationDate).toBe('2099-01-01');
      expect(vars.optionType).toBe('call');
      expect(vars.riskFreeRate).toBe(0.045);
      expect(vars.spotMin).toBeCloseTo(590 * 0.8, 6);
      expect(vars.spotMax).toBeCloseTo(590 * 1.2, 6);
      expect(vars.numPoints).toBe(100);

      req.flush({
        data: {
          pricingModelComparison: {
            success: true,
            strike: 590, optionType: 'call', expirationDate: '2099-01-01',
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

      expect(component.serverResult()).not.toBeNull();
      expect(component.serverResult()!.models.length).toBe(2);
      expect(component.compareLoading()).toBe(false);
      expect(component.statusMessage()?.type).toBe('success');
    });

    it('records server-level errors in statusMessage and clears serverResult', async () => {
      seedReadyState();
      // Pre-seed a result so we can assert it gets cleared
      component.serverResult.set({ success: true, strike: 0, optionType: 'call', expirationDate: '', timeToExpiryYears: 0, models: [], error: null });

      const promise = component.runComparison();
      const req = expectGraphQL(httpMock, 'pricingModelComparison');
      req.flush({
        data: {
          pricingModelComparison: {
            success: false,
            strike: 590, optionType: 'call', expirationDate: '2099-01-01',
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
      component.selectedExpiration.set('2099-01-01');
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
