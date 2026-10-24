import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { ActivatedRoute, convertToParamMap, provideRouter } from '@angular/router';
import { render, screen, waitFor } from '@testing-library/angular';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting, type TestRequest } from '@angular/common/http/testing';
import { vi } from 'vitest';
import { OptionsPricingComponent } from './options-pricing.component';
import { InstrumentContextService } from '../../instrument-context.service';
import { etIsoDate } from '../../../shared/date/et-midnight';
import { optionExpiryAnchorMs } from '../../../shared/option-contract/option-expiry-date';
import { PricingCompareResult, SnapshotContractResult } from '../../../services/market-data.types';
import { bsPrice } from '../../../utils/black-scholes';
import type { components } from '../../../api/broker.types';
import { environment } from '../../../../environments/environment';

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

type Schema = components['schemas'];

const API = `${environment.pythonServiceUrl}/api`;

/** The one request posted to a Python route; throws unless exactly one is pending. */
function expectPost(httpMock: HttpTestingController, route: string): TestRequest {
  const matches = httpMock.match(`${API}/${route}`);
  if (matches.length !== 1) {
    throw new Error(`Expected exactly one POST to /api/${route}; found ${matches.length}`);
  }
  return matches[0];
}

function expirationsReply(expirations: number[]): Schema['OptionsExpirationsResponse'] {
  return { success: true, expirations, count: expirations.length, error: null };
}

/** A call contract in the wire shape, `buildContract`'s twin. */
function wireContract(strike = 590): NonNullable<Schema['OptionsChainSnapshotResponse']['contracts']>[number] {
  return {
    ticker: 'O:SPY260220C00590000',
    contract_type: 'call',
    strike_price: strike,
    expiration_ms: 1_771_621_200_000,
    break_even_price: 595,
    implied_volatility: 0.20,
    open_interest: 1000,
    greeks: { delta: 0.5, gamma: 0.02, theta: -0.05, vega: 0.15 },
    day: { open: 5, high: 6, low: 4.5, close: 5.5, volume: 1000, vwap: null },
    last_trade: null,
    last_quote: null,
  };
}

function chainReply(overrides: Partial<Schema['OptionsChainSnapshotResponse']> = {}): Schema['OptionsChainSnapshotResponse'] {
  return {
    success: true,
    underlying: { ticker: 'SPY', price: 590, change: 0, change_percent: 0 },
    contracts: [],
    count: 0,
    risk_free_rate: null,
    dividend_yield: null,
    rate_source: null,
    dividend_source: null,
    error: null,
    ...overrides,
  };
}

const PYTHON_BS_POINT = { spot: 590, price: 5.5, delta: 0.5, gamma: 0.02, theta: -0.05, vega: 0.15, rho: 0.05 };

function compareReply(overrides: Partial<Schema['PricingCompareResponse']> = {}): Schema['PricingCompareResponse'] {
  return {
    success: true,
    strike: 590,
    option_type: 'call',
    expiration_ms: 4_070_984_400_000,
    time_to_expiry_years: 0.0822,
    risk_free_rate: 0.043,
    models: [{ model: 'python_bs', points: [PYTHON_BS_POINT] }],
    unavailable_models: [],
    error: null,
    ...overrides,
  };
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

/** The place the view sits in, reduced to the one thing it reads: the instrument. */
function instrumentOf(symbol: string) {
  const current = signal(symbol);
  return { current, provider: { provide: InstrumentContextService, useValue: { symbol: current } } };
}

describe('OptionsPricingComponent', () => {
  let component: OptionsPricingComponent;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      imports: [OptionsPricingComponent],
      providers: [
        provideRouter([]),
        provideHttpClient(),
        provideHttpClientTesting(),
        // These tests drive the loads by hand, so the place holds no instrument that would start one.
        instrumentOf('').provider,
      ],
    });
    const fixture = TestBed.createComponent(OptionsPricingComponent);
    component = fixture.componentInstance;
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.match(() => true).forEach(req => {
      if (!req.cancelled) req.flush({});
    });
    httpMock.verify();
  });

  // ── PL-A: Data-fetch prelude ───────────────────────────────────
  describe('PL-A: ticker → expirations → chain prelude', () => {
    it('selects nearest future expiration and triggers a chain fetch', async () => {
      component.ticker.set('SPY');
      const promise = component.fetchExpirations();

      const expReq = expectPost(httpMock, 'options/expirations');
      const future = optionExpiryAnchorMs(etIsoDate(Date.now() + 30 * 86400000));
      expReq.flush(expirationsReply([future]));

      // Yield a full event-loop turn so the awaited fetchChain() can issue its request.
      await new Promise(resolve => setTimeout(resolve, 0));

      const chainReq = expectPost(httpMock, 'snapshot/options-chain');
      chainReq.flush(chainReply({
        underlying: { ticker: 'SPY', price: 590, change: 1, change_percent: 0.17 },
        contracts: [wireContract()],
        count: 1,
        risk_free_rate: 0.045,
        dividend_yield: 0.012,
        rate_source: 'fred',
        dividend_source: 'polygon',
      }));

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
      expectPost(httpMock, 'snapshot/options-chain').flush(chainReply({ risk_free_rate: 0, rate_source: 'FRED' }));
      await promise;

      expect(component.riskFreeRate()).toBe(0);
    });

    it('clears selected contract and prior server result when fetchChain runs again', async () => {
      // Seed prior state
      component.selectedContract.set(buildContract());
      component.serverResult.set({
        success: true, strike: 0, optionType: 'call', expirationMs: 0,
        timeToExpiryYears: 0, riskFreeRate: null, models: [], unavailableModels: [], error: null,
      } satisfies PricingCompareResult);

      const promise = component.fetchChain('AAPL', 4_070_984_400_000);

      // Synchronous reset
      expect(component.selectedContract()).toBeNull();
      expect(component.serverResult()).toBeNull();
      expect(component.chainLoading()).toBe(true);

      const chainReq = expectPost(httpMock, 'snapshot/options-chain');
      chainReq.flush(chainReply({ underlying: { ticker: 'AAPL', price: 230, change: 0, change_percent: 0 } }));

      await promise;
      expect(component.chainLoading()).toBe(false);
    });

    // ── Failures and stale data are shown, not hidden (#3221) ──
    function strikesReply(price: number, strikes: number[]): Schema['OptionsChainSnapshotResponse'] {
      return chainReply({
        underlying: { ticker: 'SPY', price, change: 0, change_percent: 0 },
        contracts: strikes.map(strike => wireContract(strike)),
        count: strikes.length,
      });
    }

    function pendingFor(route: string, key: 'underlying_ticker' | 'expiration_ms', value: string | number) {
      const found = httpMock.match(r => {
        const b = r.body as Record<string, unknown> | null;
        return r.url === `${API}/${route}` && b?.[key] === value;
      });
      expect(found.length).toBe(1);
      return found[0];
    }

    it('says why the expirations could not be loaded', async () => {
      component.ticker.set('SPY');
      const promise = component.fetchExpirations();

      expectPost(httpMock, 'options/expirations').flush(
        { detail: 'Failed to list options expirations: Polygon rate limited' },
        { status: 500, statusText: 'Internal Server Error' },
      );
      await promise;

      expect(component.statusMessage()?.type).toBe('error');
      expect(component.statusMessage()?.text).toContain('Polygon rate limited');
      expect(component.expirationsLoading()).toBe(false);
    });

    it('says why the chain could not be loaded', async () => {
      const promise = component.fetchChain('SPY', 4_070_984_400_000);
      expectPost(httpMock, 'snapshot/options-chain').flush(
        { detail: 'Snapshot unavailable' },
        { status: 500, statusText: 'Internal Server Error' },
      );
      await promise;

      expect(component.statusMessage()?.type).toBe('error');
      expect(component.statusMessage()?.text).toContain('Snapshot unavailable');
      expect(component.chainLoading()).toBe(false);
    });

    it('leaves no strikes from the previous expiry to pick when the chain request fails', async () => {
      component.allContracts.set([buildContract()]);

      const promise = component.fetchChain('SPY', 4_070_984_400_000);
      expectPost(httpMock, 'snapshot/options-chain').error(new ProgressEvent('error'));
      await promise;

      expect(component.statusMessage()?.type).toBe('error');
      expect(component.allContracts()).toEqual([]);
    });

    it('keeps the later expiry\'s chain when the earlier expiry\'s reply lands last', async () => {
      const early = 4_070_984_400_000;
      const late = 4_073_576_400_000;
      const first = component.fetchChain('SPY', early);
      const second = component.fetchChain('SPY', late);

      pendingFor('snapshot/options-chain', 'expiration_ms', late).flush(strikesReply(600, [600]));
      await second;
      pendingFor('snapshot/options-chain', 'expiration_ms', early).flush(strikesReply(500, [500]));
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

      pendingFor('options/expirations', 'underlying_ticker', 'MSFT').flush(expirationsReply([msftExpiry]));
      await new Promise(resolve => setTimeout(resolve, 0));
      pendingFor('snapshot/options-chain', 'underlying_ticker', 'MSFT').flush(strikesReply(400, [400]));
      await second;

      pendingFor('options/expirations', 'underlying_ticker', 'AAPL').flush(expirationsReply([aaplExpiry]));
      await first;

      expect(component.availableExpirations()).toEqual([msftExpiry]);
      expect(component.selectedExpiration()).toBe(msftExpiry);
      expect(component.allContracts().map(c => c.strikePrice)).toEqual([400]);
      expect(component.expirationsLoading()).toBe(false);
    });
  });

  // ── PL-B: Compare workflow ─────────────────────────────────────
  describe('PL-B: runComparison → pricing comparison call', () => {
    function seedReadyState(): void {
      component.ticker.set('SPY');
      component.selectedExpiration.set(4_070_984_400_000);
      component.underlying.set({ ticker: 'SPY', price: 590 });
      component.allContracts.set([buildContract()]);
      component.selectedContract.set(buildContract());
    }

    it('sends (spot, strike, vol, expiration, optionType, riskFreeRate, range, numPoints) in the wire shape', async () => {
      seedReadyState();
      component.riskFreeRate.set(0.045);
      component.spotRangePct.set(20);

      const promise = component.runComparison();
      expect(component.compareLoading()).toBe(true);

      const req = expectPost(httpMock, 'quantlib/compare');
      const body = req.request.body as Schema['PricingCompareRequest'];
      expect(body.spot).toBe(590);
      expect(body.strike).toBe(590);
      expect(body.volatility).toBe(0.20);
      expect(body.expiration_ms).toBe(4_070_984_400_000);
      expect(body.option_type).toBe('call');
      expect(body.risk_free_rate).toBe(0.045);
      expect(body.spot_min).toBeCloseTo(590 * 0.8, 6);
      expect(body.spot_max).toBeCloseTo(590 * 1.2, 6);
      expect(body.num_points).toBe(100);

      req.flush(compareReply({
        models: [
          { model: 'python_bs', points: [PYTHON_BS_POINT] },
          { model: 'quantlib_bs', points: [{ ...PYTHON_BS_POINT, price: 5.499 }] },
        ],
      }));

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
      const req = expectPost(httpMock, 'quantlib/compare');
      expect(req.request.body.risk_free_rate).toBeUndefined();

      req.flush(compareReply({ success: false, time_to_expiry_years: 0, models: [], error: 'stop' }));
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
        timeToExpiryYears: ttm, riskFreeRate: serverRate, unavailableModels: [], error: null,
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
      component.serverResult.set({ success: true, strike: 0, optionType: 'call', expirationMs: 0, timeToExpiryYears: 0, riskFreeRate: null, models: [], unavailableModels: [], error: null });

      const promise = component.runComparison();
      const req = expectPost(httpMock, 'quantlib/compare');
      req.flush(compareReply({ success: false, time_to_expiry_years: 0, models: [], error: 'QuantLib not available' }));
      await promise;

      expect(component.statusMessage()?.type).toBe('error');
      expect(component.statusMessage()?.text).toContain('QuantLib not available');
      expect(component.serverResult()).toBeNull();
      expect(component.compareLoading()).toBe(false);
    });

    it('shows the server reason for an expired option, not a timeout guess', async () => {
      seedReadyState();

      const promise = component.runComparison();
      const req = expectPost(httpMock, 'quantlib/compare');
      req.flush(compareReply({ success: false, time_to_expiry_years: 0, models: [], error: 'Option has expired' }));
      await promise;

      expect(component.statusMessage()?.type).toBe('error');
      expect(component.statusMessage()?.text).toContain('Option has expired');
      expect(component.statusMessage()?.text).not.toContain('timed out');
    });

    it.each([null, 0, -5, 100])('sends nothing and says why when the range is %s percent', async (rangePct) => {
      seedReadyState();
      component.spotRangePct.set(rangePct as number);

      await component.runComparison();

      expect(component.statusMessage()?.type).toBe('warn');
      expect(component.statusMessage()?.text).toContain('Range %');
      expect(httpMock.match(`${API}/quantlib/compare`).length).toBe(0);
    });

    it('draws no Legacy BS overlay while Range % is empty', () => {
      seedReadyState();
      component.spotRangePct.set(null as unknown as number);
      component.serverResult.set({
        success: true, strike: 590, optionType: 'call', expirationMs: 4_070_984_400_000,
        timeToExpiryYears: 0.25, riskFreeRate: 0.043, unavailableModels: [], error: null,
        models: [{
          model: 'python_bs',
          points: [{ spot: 590, price: 20, delta: 0.5, gamma: 0.01, theta: -0.1, vega: 0.2, rho: 0.1 }],
        }],
      });

      expect(component.summaryStats()).toEqual([]);
    });

    it('says how many engines ran and why each other did not', async () => {
      seedReadyState();

      const promise = component.runComparison();
      expectPost(httpMock, 'quantlib/compare').flush(compareReply({
        models: [
          { model: 'python_bs', points: [PYTHON_BS_POINT] },
          { model: 'quantlib_bs', points: [PYTHON_BS_POINT] },
        ],
        unavailable_models: [
          { model: 'quantlib_crr', reason: 'the engine raised RuntimeError while pricing this contract' },
          { model: 'quantlib_jr', reason: 'the engine raised RuntimeError while pricing this contract' },
          { model: 'quantlib_mc', reason: 'the engine raised ValueError while pricing this contract' },
        ],
      }));
      await promise;

      const status = component.statusMessage();
      expect(status?.type).toBe('warn');
      expect(status?.text).toContain('2 of 5 server engines');
      expect(status?.text).toContain('CRR, JR: the engine raised RuntimeError');
      expect(status?.text).toContain('MC: the engine raised ValueError');
      expect(component.serverResult()?.models.length).toBe(2);
    });

    it('with QuantLib absent names every QuantLib engine once, under the one reason', async () => {
      seedReadyState();

      const promise = component.runComparison();
      expectPost(httpMock, 'quantlib/compare').flush(compareReply({
        unavailable_models: ['quantlib_bs', 'quantlib_crr', 'quantlib_jr', 'quantlib_lr', 'quantlib_fd', 'quantlib_mc']
          .map(model => ({ model, reason: 'QuantLib is not installed on the data service' })),
      }));
      await promise;

      const text = component.statusMessage()?.text ?? '';
      expect(text).toContain('1 of 7 server engines');
      expect(text.match(/QuantLib is not installed on the data service/g)?.length).toBe(1);
      expect(text).toContain('QL-A, CRR, JR, LR, FD, MC');
    });

    it('reports every engine as loaded, without a warning, when none is unavailable', async () => {
      seedReadyState();

      const promise = component.runComparison();
      expectPost(httpMock, 'quantlib/compare').flush(compareReply());
      await promise;

      expect(component.statusMessage()?.type).toBe('success');
      expect(component.statusMessage()?.text).toContain('1 of 1 server engines');
    });

    describe('a Greek the engine does not provide', () => {
      const SPOT = 590;
      const BS_PRICE = 5.5;

      function seedMonteCarloWithoutTheta(): void {
        seedReadyState();
        component.riskFreeRate.set(0.043);
        component.serverResult.set({
          success: true, strike: 590, optionType: 'call', expirationMs: 4_070_984_400_000,
          timeToExpiryYears: 0.25, riskFreeRate: 0.043, unavailableModels: [], error: null,
          models: [
            { model: 'python_bs', points: [{ spot: SPOT, price: BS_PRICE, delta: 0.5, gamma: 0.02, theta: -0.05, vega: 0.15, rho: 0.05 }] },
            { model: 'quantlib_mc', points: [{ spot: SPOT, price: BS_PRICE + 0.1, delta: 0.52, gamma: 0.021, theta: null, vega: null, rho: null }] },
          ],
        });
        component.diffReference.set('python_bs');
      }

      it('is left out of the difference table (not compared as zero), compared where the engine does provide the Greek, and names the engine that has none', () => {
        seedMonteCarloWithoutTheta();

        component.selectedMetric.set('theta');
        const rows = component.summaryStats().map(row => row.key);
        expect.soft(rows, 'theta: leaves the engine without it out of the difference table, not compared as zero').not.toContain('quantlib_mc');
        expect.soft(rows, 'theta: still lists the legacy_bs overlay').toContain('legacy_bs');

        component.selectedMetric.set('delta');
        const mc = component.summaryStats().find(row => row.key === 'quantlib_mc');
        expect.soft(mc?.maxDiff, 'delta: compares the engine where it does provide the Greek').toBeCloseTo(0.02, 12);

        component.selectedMetric.set('vega');
        expect.soft(component.metricNotProvidedBy(), 'vega: names the engine that has none for the selected Greek').toEqual(['MC']);

        component.selectedMetric.set('delta');
        expect.soft(component.metricNotProvidedBy(), 'delta: names none for price or delta').toEqual([]);
      });
    });

    it('says why the comparison failed when Python answers an HTTP failure', async () => {
      seedReadyState();

      const promise = component.runComparison();
      expectPost(httpMock, 'quantlib/compare').flush(
        { detail: [{ loc: ['body', 'volatility'], msg: 'Input should be greater than 0', type: 'greater_than' }] },
        { status: 422, statusText: 'Unprocessable Entity' },
      );

      await promise;

      expect(component.statusMessage()?.type).toBe('error');
      expect(component.statusMessage()?.text).toContain('Input should be greater than 0');
      expect(component.serverResult()).toBeNull();
      expect(component.compareLoading()).toBe(false);
    });

    it('catches network errors and surfaces them in statusMessage', async () => {
      seedReadyState();

      const promise = component.runComparison();
      const req = expectPost(httpMock, 'quantlib/compare');
      req.error(new ProgressEvent('error'));

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
      expect(httpMock.match(`${API}/quantlib/compare`).length).toBe(0);
    });

    it('runComparison sets a warn message when underlying price is missing', async () => {
      component.underlying.set(null);
      component.selectedContract.set(buildContract());

      await component.runComparison();

      expect(component.statusMessage()?.type).toBe('warn');
      expect(httpMock.match(`${API}/quantlib/compare`).length).toBe(0);
    });

    it('runComparison reports the missing fields when the contract is incomplete', async () => {
      component.underlying.set({ ticker: 'SPY', price: 590 });
      component.selectedExpiration.set(4_070_984_400_000);
      // Contract missing implied volatility — the component should call out which field is missing
      component.selectedContract.set(buildContract({ impliedVolatility: null }));

      await component.runComparison();

      expect(component.statusMessage()?.type).toBe('warn');
      expect(component.statusMessage()?.text).toContain('implied volatility');
      expect(httpMock.match(`${API}/quantlib/compare`).length).toBe(0);
    });
  });
});

describe('OptionsPricingComponent selected contract info', () => {
  it.each([
    [0.2, 'IV: 20.0%'],
    [null, 'IV: —'],
  ])('shows implied volatility %s as "%s"', async (impliedVolatility, expected) => {
    const { fixture, container } = await render(OptionsPricingComponent, {
      providers: [
        provideRouter([]),
        provideHttpClient(),
        provideHttpClientTesting(),
        instrumentOf('SPY').provider,
      ],
    });

    fixture.componentInstance.selectedContract.set(buildContract({ impliedVolatility }));
    fixture.detectChanges();

    const info = container.querySelector('.contract-info');
    expect(info?.textContent?.replace(/\s+/g, ' ')).toContain(expected);
  });
});

describe('OptionsPricingComponent under the open instrument', () => {
  const NEAR = optionExpiryAnchorMs(etIsoDate(Date.now() + 10 * 86_400_000));
  const FAR = optionExpiryAnchorMs(etIsoDate(Date.now() + 40 * 86_400_000));

  async function open(symbol: string, expiry: string | null = null) {
    const instrument = instrumentOf(symbol);
    const rendered = await render(OptionsPricingComponent, {
      providers: [
        provideRouter([]),
        provideHttpClient(),
        provideHttpClientTesting(),
        instrument.provider,
        {
          provide: ActivatedRoute,
          useValue: { snapshot: { queryParamMap: convertToParamMap(expiry === null ? {} : { expiry }) } },
        },
      ],
    });
    return { ...rendered, instrument, http: TestBed.inject(HttpTestingController) };
  }

  /** The one pending expirations request for `ticker`, flushed with `expirations`. */
  async function answerExpirations(http: HttpTestingController, ticker: string, expirations: number[]) {
    const pending = await waitFor(() => {
      const matches = http.match((request) => request.url === `${API}/options/expirations` && request.body.underlying_ticker === ticker);
      expect(matches).toHaveLength(1);
      return matches[0];
    });
    pending.flush(expirationsReply(expirations));
  }

  /** The chain request for `ticker` at `expirationMs`, flushed with an empty chain. */
  async function answerChain(http: HttpTestingController, ticker: string, expirationMs: number) {
    const pending = await waitFor(() => {
      const matches = http.match(
        (request) => request.url === `${API}/snapshot/options-chain`
          && request.body.underlying_ticker === ticker && request.body.expiration_ms === expirationMs,
      );
      expect(matches).toHaveLength(1);
      return matches[0];
    });
    pending.flush(chainReply({ underlying: { ticker, price: 100, change: 0, change_percent: 0 } }));
  }

  it('loads the open instrument\'s expirations on its own, and the next instrument\'s when another opens', async () => {
    const { instrument, http, fixture } = await open('SPY');
    await answerExpirations(http, 'SPY', [NEAR, FAR]);
    await answerChain(http, 'SPY', NEAR);
    expect(fixture.componentInstance.ticker()).toBe('SPY');

    instrument.current.set('QQQ');
    fixture.detectChanges();
    await answerExpirations(http, 'QQQ', [FAR]);
    await answerChain(http, 'QQQ', FAR);

    expect(fixture.componentInstance.ticker()).toBe('QQQ');
    expect(fixture.componentInstance.selectedExpiration()).toBe(FAR);
    http.verify();
  });

  it('opens on the expiry the address names when the chain lists it', async () => {
    const { http, fixture } = await open('SPY', String(FAR));
    await answerExpirations(http, 'SPY', [NEAR, FAR]);
    await answerChain(http, 'SPY', FAR);

    await waitFor(() => expect(fixture.componentInstance.expirationsLoading()).toBe(false));
    expect(fixture.componentInstance.selectedExpiration()).toBe(FAR);
    expect(fixture.componentInstance.statusMessage()).toBeNull();
    http.verify();
  });

  it.each([
    ['one the chain does not list', String(FAR + 86_400_000), /is not an expiration of SPY/],
    ['text that is no expiry anchor', 'next-friday', /"next-friday"\) is not an expiration of SPY/],
  ])('opens on the nearest expiry and says so when the address names %s', async (_name, expiry, message) => {
    const { http, fixture } = await open('SPY', expiry);
    await answerExpirations(http, 'SPY', [NEAR, FAR]);
    await answerChain(http, 'SPY', NEAR);

    await waitFor(() => expect(fixture.componentInstance.statusMessage()?.type).toBe('warn'));
    expect(fixture.componentInstance.selectedExpiration()).toBe(NEAR);
    expect(fixture.componentInstance.statusMessage()?.text).toMatch(message);
    expect(screen.getByText(message)).toBeTruthy();
    http.verify();
  });
});
