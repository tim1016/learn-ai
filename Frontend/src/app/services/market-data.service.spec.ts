import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { firstValueFrom } from 'rxjs';
import { MarketDataService } from './market-data.service';
import { createMockAggregate } from '../../testing/factories/market-data.factory';
import { environment } from '../../environments/environment';

const API = `${environment.pythonServiceUrl}/api`;

describe('MarketDataService', () => {
  let service: MarketDataService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    service = TestBed.inject(MarketDataService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => httpMock.verify());

  describe('getStockAggregates', () => {
    it('posts the bars request and returns the bars under the upper-cased ticker', async () => {
      const aggregate = createMockAggregate();

      const promise = firstValueFrom(service.getStockAggregates('msft', '2026-01-01', '2026-06-30', 'hour', 4));

      const req = httpMock.expectOne(`${API}/aggregates/fetch`);
      expect(req.request.method).toBe('POST');
      expect(req.request.body).toEqual({
        ticker: 'msft',
        from_date: '2026-01-01',
        to_date: '2026-06-30',
        timespan: 'hour',
        multiplier: 4,
        adjusted: true,
      });
      req.flush({
        success: true,
        data: [aggregate],
        summary: { original_count: 1, cleaned_count: 1, removed_count: 0, removal_percentage: 0 },
        ticker: 'msft',
        data_type: 'aggregates',
      });

      expect(await promise).toEqual({ ticker: 'MSFT', aggregates: [aggregate] });
    });

    it('rejects with the service\'s reason when the fetch fails', async () => {
      const promise = firstValueFrom(service.getStockAggregates('BAD', '2026-01-01', '2026-01-31'));

      httpMock.expectOne(`${API}/aggregates/fetch`).flush(
        { detail: 'Failed to fetch aggregates: Ticker not found' },
        { status: 500, statusText: 'Internal Server Error' },
      );

      await expect(promise).rejects.toThrow('Failed to fetch aggregates: Ticker not found');
    });
  });

  describe('getOptionsExpirations', () => {
    it('asks for the given contract type and anchor-instant bounds', async () => {
      const promise = firstValueFrom(service.getOptionsExpirations('SPY', {
        contractType: 'call', expirationMsGte: 1_774_123_200_000, expirationMsLte: 1_790_000_000_000,
      }));

      const req = httpMock.expectOne(`${API}/options/expirations`);
      expect(req.request.body).toEqual({
        underlying_ticker: 'SPY',
        contract_type: 'call',
        expiration_ms_gte: 1_774_123_200_000,
        expiration_ms_lte: 1_790_000_000_000,
      });
      req.flush({ success: true, expirations: [1_774_123_200_000], count: 1, error: null });

      expect(await promise).toEqual([1_774_123_200_000]);
    });

    it('rejects with the reason when Python answers an HTTP failure, instead of reading as no options', async () => {
      const promise = firstValueFrom(service.getOptionsExpirations('SPY'));

      httpMock.expectOne(`${API}/options/expirations`).flush(
        { detail: 'Failed to list options expirations: Polygon rate limited' },
        { status: 500, statusText: 'Internal Server Error' },
      );

      await expect(promise).rejects.toThrow(
        'Option expirations unavailable: Failed to list options expirations: Polygon rate limited',
      );
    });

    it('rejects with the vendor error when the answer reports a failure', async () => {
      const promise = firstValueFrom(service.getOptionsExpirations('SPY'));

      httpMock.expectOne(`${API}/options/expirations`).flush({
        success: false, expirations: [], count: 0, error: 'Polygon rate limited',
      });

      await expect(promise).rejects.toThrow('Polygon rate limited');
    });

    it('resolves an empty list for a ticker with no listed options', async () => {
      const promise = firstValueFrom(service.getOptionsExpirations('BRK.A'));

      httpMock.expectOne(`${API}/options/expirations`).flush({
        success: true, expirations: [], count: 0, error: null,
      });

      expect(await promise).toEqual([]);
    });
  });

  describe('getOptionsChainSnapshot', () => {
    it('posts the underlying and expiry and maps the chain to the screens\' camelCase shape', async () => {
      const promise = firstValueFrom(service.getOptionsChainSnapshot('AAPL', 1_774_123_200_000));

      const req = httpMock.expectOne(`${API}/snapshot/options-chain`);
      expect(req.request.body).toEqual({ underlying_ticker: 'AAPL', expiration_ms: 1_774_123_200_000 });
      req.flush({
        success: true,
        underlying: { ticker: 'AAPL', price: 150, change: 2, change_percent: 1.35 },
        contracts: [{
          ticker: 'O:AAPL260320C00150000',
          contract_type: 'call',
          strike_price: 150,
          expiration_ms: 1_774_123_200_000,
          break_even_price: 155,
          implied_volatility: 0.25,
          open_interest: 1200,
          greeks: { delta: 0.52, gamma: 0.03, theta: -0.05, vega: 0.11 },
          day: { open: 4.8, high: 5.4, low: 4.7, close: 5.1, volume: 300, vwap: 5.0 },
          last_trade: { price: 5.1, size: 2, exchange: 304, timeframe: 'DELAYED', sip_timestamp: 1, conditions: [1] },
          last_quote: { bid: 5.0, ask: 5.2, bid_size: 10, ask_size: 12, midpoint: 5.1, timeframe: 'DELAYED', last_updated: 2 },
        }],
        count: 1,
        risk_free_rate: 0.043,
        dividend_yield: 0.005,
        rate_source: 'FRED',
        dividend_source: 'polygon',
        error: null,
      });

      const result = await promise;
      expect(result).toMatchObject({
        success: true,
        underlying: { ticker: 'AAPL', price: 150, change: 2, changePercent: 1.35 },
        count: 1,
        riskFreeRate: 0.043,
        dividendYield: 0.005,
        rateSource: 'FRED',
        dividendSource: 'polygon',
        error: null,
      });
      expect(result.contracts[0]).toMatchObject({
        ticker: 'O:AAPL260320C00150000',
        contractType: 'call',
        strikePrice: 150,
        expirationMs: 1_774_123_200_000,
        breakEvenPrice: 155,
        impliedVolatility: 0.25,
        openInterest: 1200,
        greeks: { delta: 0.52, gamma: 0.03, theta: -0.05, vega: 0.11 },
        day: { close: 5.1, volume: 300 },
        lastTrade: { price: 5.1, size: 2, exchange: 304, timeframe: 'DELAYED' },
        lastQuote: { bid: 5.0, ask: 5.2, bidSize: 10, askSize: 12, midpoint: 5.1, timeframe: 'DELAYED' },
      });
    });

    it('resolves success: false with the service\'s reason when Python answers an HTTP failure', async () => {
      const promise = firstValueFrom(service.getOptionsChainSnapshot('AAPL'));

      httpMock.expectOne(`${API}/snapshot/options-chain`).flush(
        { detail: 'Failed to fetch options chain snapshot: Snapshot unavailable' },
        { status: 500, statusText: 'Internal Server Error' },
      );

      const result = await promise;
      expect(result.success).toBe(false);
      expect(result.error).toBe('Failed to fetch options chain snapshot: Snapshot unavailable');
      expect(result.contracts).toEqual([]);
    });
  });

  describe('getStockSnapshot', () => {
    it('posts the ticker and maps the snapshot to the screens\' camelCase shape', async () => {
      const promise = firstValueFrom(service.getStockSnapshot('AAPL'));

      const req = httpMock.expectOne(`${API}/snapshot/ticker`);
      expect(req.request.body).toEqual({ ticker: 'AAPL' });
      req.flush({
        success: true,
        snapshot: {
          ticker: 'AAPL',
          day: { open: 149, high: 152, low: 148, close: 151, volume: 5000, vwap: 150.2 },
          prev_day: { open: 147, high: 150, low: 146, close: 148.5, volume: 4800, vwap: 148.1 },
          min: { open: 151, high: 151.2, low: 150.9, close: 151.1, volume: 80, vwap: 151.05, accumulated_volume: 5000, timestamp: 1_774_123_200_000 },
          todays_change: 2.5,
          todays_change_percent: 1.68,
          updated: 1_774_123_260_000,
        },
        error: null,
      });

      expect(await promise).toEqual({
        success: true,
        snapshot: {
          ticker: 'AAPL',
          day: { open: 149, high: 152, low: 148, close: 151, volume: 5000, vwap: 150.2 },
          prevDay: { open: 147, high: 150, low: 146, close: 148.5, volume: 4800, vwap: 148.1 },
          min: { open: 151, high: 151.2, low: 150.9, close: 151.1, volume: 80, vwap: 151.05, accumulatedVolume: 5000, timestamp: 1_774_123_200_000 },
          todaysChange: 2.5,
          todaysChangePercent: 1.68,
          updated: 1_774_123_260_000,
        },
        error: null,
      });
    });

    it('resolves success: false with the service\'s reason when Python answers an HTTP failure', async () => {
      const promise = firstValueFrom(service.getStockSnapshot('BAD'));

      httpMock.expectOne(`${API}/snapshot/ticker`).flush(
        { detail: 'Failed to fetch stock snapshot: Snapshot unavailable' },
        { status: 500, statusText: 'Internal Server Error' },
      );

      expect(await promise).toEqual({
        success: false, snapshot: null, error: 'Failed to fetch stock snapshot: Snapshot unavailable',
      });
    });

    it('resolves success: false, with a reason, when Python cannot be reached', async () => {
      const promise = firstValueFrom(service.getStockSnapshot('AAPL'));

      httpMock.expectOne(`${API}/snapshot/ticker`).error(new ProgressEvent('error'));

      const result = await promise;
      expect(result.success).toBe(false);
      expect(result.error).toContain(`${API}/snapshot/ticker`);
    });
  });

  describe('analyzeOptionsStrategy', () => {
    const LEGS = [
      { legId: 'L1', strike: 100, optionType: 'call', position: 'long', premium: 5, iv: 0.3, quantity: 2 },
      { strike: 105, optionType: 'call', position: 'short', premium: 3, iv: 0.28 },
    ];

    // Python owns the default rate: an unset rate is left out of the body (Python
    // rejects a null) so it fills its one default (#2764).
    it('sends no rate unless given one, and names every field the way Python does', async () => {
      const promise = firstValueFrom(service.analyzeOptionsStrategy('SPY', LEGS, 4_070_984_400_000, 100));

      const req = httpMock.expectOne(`${API}/strategy/analyze`);
      expect(req.request.body.risk_free_rate).toBeUndefined();
      expect(req.request.body).toEqual({
        symbol: 'SPY',
        legs: [
          { leg_id: 'L1', strike: 100, option_type: 'call', position: 'long', premium: 5, iv: 0.3, quantity: 2 },
          { strike: 105, option_type: 'call', position: 'short', premium: 3, iv: 0.28 },
        ],
        expiration_ms: 4_070_984_400_000,
        spot_price: 100,
        include_current_curve: false,
        include_greek_curves: false,
        include_leg_diagnostics: false,
        what_if_time_shift_days: 0,
        what_if_iv_shift: 0,
      });
      req.flush({ success: true });
      await promise;
    });

    it('sends the rate and the opt-in flags it was given', async () => {
      const promise = firstValueFrom(service.analyzeOptionsStrategy('SPY', LEGS, 4_070_984_400_000, 100, 0, {
        includeCurrentCurve: true, includeGreekCurves: true, includeLegDiagnostics: true,
        whatIfTimeShiftDays: 7, whatIfIvShift: 0.05,
      }));

      const req = httpMock.expectOne(`${API}/strategy/analyze`);
      expect(req.request.body).toMatchObject({
        risk_free_rate: 0,
        include_current_curve: true,
        include_greek_curves: true,
        include_leg_diagnostics: true,
        what_if_time_shift_days: 7,
        what_if_iv_shift: 0.05,
      });
      req.flush({ success: true });
      await promise;
    });

    it('maps the analysis, curves and leg diagnostics to the screens\' camelCase shape', async () => {
      const promise = firstValueFrom(service.analyzeOptionsStrategy('SPY', LEGS, 4_070_984_400_000, 100));

      httpMock.expectOne(`${API}/strategy/analyze`).flush({
        success: true,
        symbol: 'SPY',
        spot_price: 100,
        strategy_cost: 2,
        pop: 0.45,
        expected_value: 12.5,
        max_profit: 300,
        max_loss: -200,
        breakevens: [102],
        curve: [{ price: 90, pnl: -200 }, { price: 110, pnl: 300 }],
        greeks: { delta: 0.1, gamma: 0.02, theta: -0.03, vega: 0.04 },
        current_curve: [{ price: 90, theoretical_value: 1.5, theoretical_pnl: -50 }],
        greek_curves: [{ price: 90, delta: 0.1, gamma: 0.02, theta: -0.03, vega: 0.04 }],
        leg_diagnostics: [{
          leg_id: 'L1', strike: 100, option_type: 'call', position: 'long', quantity: 2, iv: 0.3,
          entry_premium: 5, current_theoretical: 5.5, current_delta: 0.5, current_gamma: 0.04,
          current_theta: -0.05, current_vega: 0.1, leg_pnl: 100,
        }],
        error: null,
      });

      expect(await promise).toEqual({
        success: true,
        symbol: 'SPY',
        spotPrice: 100,
        strategyCost: 2,
        pop: 0.45,
        expectedValue: 12.5,
        maxProfit: 300,
        maxLoss: -200,
        breakevens: [102],
        curve: [{ price: 90, pnl: -200 }, { price: 110, pnl: 300 }],
        greeks: { delta: 0.1, gamma: 0.02, theta: -0.03, vega: 0.04 },
        currentCurve: [{ price: 90, theoreticalValue: 1.5, theoreticalPnl: -50 }],
        greekCurves: [{ price: 90, delta: 0.1, gamma: 0.02, theta: -0.03, vega: 0.04 }],
        legDiagnostics: [{
          legId: 'L1', strike: 100, optionType: 'call', position: 'long', quantity: 2, iv: 0.3,
          entryPremium: 5, currentTheoretical: 5.5, currentDelta: 0.5, currentGamma: 0.04,
          currentTheta: -0.05, currentVega: 0.1, legPnl: 100,
        }],
        error: null,
      });
    });

    it('leaves the opt-in curves null when Python sends none', async () => {
      const promise = firstValueFrom(service.analyzeOptionsStrategy('SPY', LEGS, 4_070_984_400_000, 100));

      httpMock.expectOne(`${API}/strategy/analyze`).flush({
        success: true, current_curve: null, greek_curves: null, leg_diagnostics: null,
      });

      const result = await promise;
      expect(result.currentCurve).toBeNull();
      expect(result.greekCurves).toBeNull();
      expect(result.legDiagnostics).toBeNull();
    });

    it('resolves success: false with the reasons Python refused the request for', async () => {
      const promise = firstValueFrom(service.analyzeOptionsStrategy('SPY', LEGS, 4_070_984_400_000, 100));

      httpMock.expectOne(`${API}/strategy/analyze`).flush(
        {
          detail: [
            { loc: ['body', 'risk_free_rate'], msg: 'Input should be less than or equal to 0.5', type: 'less_than_equal' },
            { loc: ['body', 'legs'], msg: 'List should have at least 1 item', type: 'too_short' },
          ],
        },
        { status: 422, statusText: 'Unprocessable Entity' },
      );

      const result = await promise;
      expect(result.success).toBe(false);
      expect(result.error).toBe('Input should be less than or equal to 0.5; List should have at least 1 item');
    });
  });

  describe('comparePricingModels', () => {
    const PARAMS = { spot: 100, strike: 105, volatility: 0.25, expirationMs: 4_070_984_400_000, optionType: 'call' };

    it('sends no rate unless given one, and names every field the way Python does', async () => {
      const promise = firstValueFrom(service.comparePricingModels({ ...PARAMS, spotMin: 80, spotMax: 120 }));

      const req = httpMock.expectOne(`${API}/quantlib/compare`);
      expect(req.request.body.risk_free_rate).toBeUndefined();
      expect(req.request.body).toEqual({
        spot: 100,
        strike: 105,
        volatility: 0.25,
        expiration_ms: 4_070_984_400_000,
        option_type: 'call',
        dividend_yield: 0,
        spot_min: 80,
        spot_max: 120,
        num_points: 100,
      });
      req.flush({ success: true, strike: 105, option_type: 'call', expiration_ms: 4_070_984_400_000, time_to_expiry_years: 0.5, risk_free_rate: 0.043, models: [] });
      await promise;
    });

    it('sends the rate it was given', async () => {
      const promise = firstValueFrom(service.comparePricingModels({ ...PARAMS, riskFreeRate: 0.05 }));

      const req = httpMock.expectOne(`${API}/quantlib/compare`);
      expect(req.request.body.risk_free_rate).toBe(0.05);
      req.flush({ success: true, strike: 105, option_type: 'call', expiration_ms: 4_070_984_400_000, time_to_expiry_years: 0.5, risk_free_rate: 0.05, models: [] });
      await promise;
    });

    it('maps the curves, the unavailable models and the rate Python priced at', async () => {
      const promise = firstValueFrom(service.comparePricingModels(PARAMS));

      httpMock.expectOne(`${API}/quantlib/compare`).flush({
        success: true,
        strike: 105,
        option_type: 'call',
        expiration_ms: 4_070_984_400_000,
        time_to_expiry_years: 0.4932,
        risk_free_rate: 0.043,
        models: [
          { model: 'python_bs', points: [{ spot: 100, price: 3.2, delta: 0.4, gamma: 0.03, theta: -0.02, vega: 0.2, rho: 0.1 }] },
          { model: 'quantlib_mc', points: [{ spot: 100, price: 3.3, delta: 0.41, gamma: 0.03, theta: null, vega: null, rho: null }] },
        ],
        unavailable_models: [{ model: 'quantlib_fd', reason: 'QuantLib not installed' }],
        error: null,
      });

      const result = await promise;
      expect(result).toMatchObject({
        success: true,
        strike: 105,
        optionType: 'call',
        expirationMs: 4_070_984_400_000,
        timeToExpiryYears: 0.4932,
        riskFreeRate: 0.043,
        unavailableModels: [{ model: 'quantlib_fd', reason: 'QuantLib not installed' }],
        error: null,
      });
      expect(result.models.map(m => m.model)).toEqual(['python_bs', 'quantlib_mc']);
      expect(result.models[1].points[0]).toMatchObject({ theta: null, vega: null, rho: null });
    });

    it('resolves success: false with the service\'s reason when Python answers an HTTP failure', async () => {
      const promise = firstValueFrom(service.comparePricingModels(PARAMS));

      httpMock.expectOne(`${API}/quantlib/compare`).flush(
        { detail: 'Pricing service overloaded' },
        { status: 503, statusText: 'Service Unavailable' },
      );

      const result = await promise;
      expect(result).toMatchObject({
        success: false, strike: 105, optionType: 'call', models: [], unavailableModels: [], error: 'Pricing service overloaded',
      });
    });
  });
});
