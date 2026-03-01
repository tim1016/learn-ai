import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { firstValueFrom } from 'rxjs';
import { MarketDataService } from './market-data.service';
import { createMockAggregate, createMockSummary, createMockIndicatorSeries } from '../../testing/factories/market-data.factory';
import { environment } from '../../environments/environment';

const GRAPHQL_URL = environment.backendUrl;

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

  describe('getOrFetchStockAggregates', () => {
    it('should send POST to GraphQL endpoint', () => {
      service.getOrFetchStockAggregates('AAPL', '2026-01-01', '2026-01-31').subscribe();

      const req = httpMock.expectOne(GRAPHQL_URL);
      expect(req.request.method).toBe('POST');
      req.flush({ data: { getOrFetchStockAggregates: { ticker: 'AAPL', aggregates: [], summary: null } } });
    });

    it('should send correct variables', () => {
      service.getOrFetchStockAggregates('MSFT', '2026-01-01', '2026-06-30', 'hour', 4).subscribe();

      const req = httpMock.expectOne(GRAPHQL_URL);
      expect(req.request.body.variables).toEqual({
        ticker: 'MSFT',
        fromDate: '2026-01-01',
        toDate: '2026-06-30',
        timespan: 'hour',
        multiplier: 4,
        forceRefresh: false,
      });
      req.flush({ data: { getOrFetchStockAggregates: { ticker: 'MSFT', aggregates: [], summary: null } } });
    });

    it('should map response to SmartAggregatesResult', async () => {
      const aggregate = createMockAggregate();
      const summary = createMockSummary();

      const promise = firstValueFrom(
        service.getOrFetchStockAggregates('AAPL', '2026-01-01', '2026-01-31')
      );

      httpMock.expectOne(GRAPHQL_URL).flush({
        data: { getOrFetchStockAggregates: { ticker: 'AAPL', aggregates: [aggregate], summary } },
      });

      const result = await promise;
      expect(result.ticker).toBe('AAPL');
      expect(result.aggregates.length).toBe(1);
      expect(result.aggregates[0].open).toBe(aggregate.open);
      expect(result.summary).toEqual(summary);
    });

    it('should throw on GraphQL errors', async () => {
      const promise = firstValueFrom(
        service.getOrFetchStockAggregates('BAD', '2026-01-01', '2026-01-31')
      );

      httpMock.expectOne(GRAPHQL_URL).flush({
        data: null,
        errors: [{ message: 'Ticker not found' }],
      });

      await expect(promise).rejects.toThrow('Ticker not found');
    });
  });

  describe('fetchStockAggregates (via runBacktest mutation)', () => {
    it('should POST backtest mutation with correct variables', () => {
      service.runBacktest('AAPL', 'sma_crossover', '2026-01-01', '2026-01-31').subscribe();

      const req = httpMock.expectOne(GRAPHQL_URL);
      expect(req.request.method).toBe('POST');
      expect(req.request.body.query).toContain('runBacktest');
      expect(req.request.body.variables).toEqual({
        ticker: 'AAPL',
        strategyName: 'sma_crossover',
        fromDate: '2026-01-01',
        toDate: '2026-01-31',
        timespan: 'minute',
        multiplier: 1,
        parametersJson: '{}',
      });
      req.flush({
        data: {
          runBacktest: {
            success: true, id: 1, strategyName: 'sma_crossover', parameters: '{}',
            totalTrades: 10, winningTrades: 6, losingTrades: 4,
            totalPnL: 150.0, maxDrawdown: -50.0, sharpeRatio: 1.2, durationMs: 500,
            trades: [], error: null,
          },
        },
      });
    });

    it('should map backtest response correctly', async () => {
      const promise = firstValueFrom(
        service.runBacktest('AAPL', 'sma_crossover', '2026-01-01', '2026-01-31')
      );

      httpMock.expectOne(GRAPHQL_URL).flush({
        data: {
          runBacktest: {
            success: true, id: 1, strategyName: 'sma_crossover', parameters: '{}',
            totalTrades: 10, winningTrades: 6, losingTrades: 4,
            totalPnL: 150.0, maxDrawdown: -50.0, sharpeRatio: 1.2, durationMs: 500,
            trades: [], error: null,
          },
        },
      });

      const result = await promise;
      expect(result.success).toBe(true);
      expect(result.totalTrades).toBe(10);
      expect(result.sharpeRatio).toBe(1.2);
    });

    it('should throw on GraphQL errors', async () => {
      const promise = firstValueFrom(
        service.runBacktest('AAPL', 'sma_crossover', '2026-01-01', '2026-01-31')
      );

      httpMock.expectOne(GRAPHQL_URL).flush({
        data: null,
        errors: [{ message: 'Backtest failed' }],
      });

      await expect(promise).rejects.toThrow('Backtest failed');
    });
  });

  describe('getOptionsChainSnapshot', () => {
    it('should send correct variables', () => {
      service.getOptionsChainSnapshot('AAPL', '2026-03-21').subscribe();

      const req = httpMock.expectOne(GRAPHQL_URL);
      expect(req.request.body.variables).toEqual({
        underlyingTicker: 'AAPL',
        expirationDate: '2026-03-21',
      });
      req.flush({
        data: {
          getOptionsChainSnapshot: {
            success: true, underlying: null, contracts: [], count: 0, error: null,
          },
        },
      });
    });

    it('should map response correctly', async () => {
      const promise = firstValueFrom(service.getOptionsChainSnapshot('AAPL'));

      httpMock.expectOne(GRAPHQL_URL).flush({
        data: {
          getOptionsChainSnapshot: {
            success: true,
            underlying: { ticker: 'AAPL', price: 150, change: 2, changePercent: 1.35 },
            contracts: [],
            count: 0,
            error: null,
          },
        },
      });

      const result = await promise;
      expect(result.success).toBe(true);
      expect(result.underlying?.ticker).toBe('AAPL');
    });
  });

  describe('getStockSnapshot', () => {
    it('should send ticker variable', () => {
      service.getStockSnapshot('AAPL').subscribe();

      const req = httpMock.expectOne(GRAPHQL_URL);
      expect(req.request.body.variables).toEqual({ ticker: 'AAPL' });
      req.flush({
        data: {
          getStockSnapshot: { success: true, snapshot: null, error: null },
        },
      });
    });

    it('should throw on GraphQL errors', async () => {
      const promise = firstValueFrom(service.getStockSnapshot('BAD'));

      httpMock.expectOne(GRAPHQL_URL).flush({
        data: null,
        errors: [{ message: 'Snapshot unavailable' }],
      });

      await expect(promise).rejects.toThrow('Snapshot unavailable');
    });
  });

  describe('getTrackedTickers', () => {
    it('should send tickers array variable', () => {
      service.getTrackedTickers(['AAPL', 'MSFT']).subscribe();

      const req = httpMock.expectOne(GRAPHQL_URL);
      expect(req.request.body.variables).toEqual({ tickers: ['AAPL', 'MSFT'] });
      req.flush({
        data: {
          getTrackedTickers: { success: true, tickers: [], count: 0, error: null },
        },
      });
    });

    it('should map response with tickers', async () => {
      const promise = firstValueFrom(service.getTrackedTickers(['AAPL']));

      httpMock.expectOne(GRAPHQL_URL).flush({
        data: {
          getTrackedTickers: {
            success: true,
            tickers: [{ ticker: 'AAPL', name: 'Apple', market: 'stocks', type: 'CS', active: true, primaryExchange: 'XNAS', currencyName: 'usd' }],
            count: 1,
            error: null,
          },
        },
      });

      const result = await promise;
      expect(result.success).toBe(true);
      expect(result.tickers.length).toBe(1);
      expect(result.tickers[0].ticker).toBe('AAPL');
    });
  });

  describe('network error handling', () => {
    it('should propagate HTTP errors', async () => {
      const promise = firstValueFrom(
        service.getOrFetchStockAggregates('AAPL', '2026-01-01', '2026-01-31')
      );

      httpMock.expectOne(GRAPHQL_URL).error(
        new ProgressEvent('error'), { status: 500, statusText: 'Internal Server Error' }
      );

      await expect(promise).rejects.toThrow();
    });
  });

  describe('calculateIndicators', () => {
    it('should send correct variables', () => {
      const indicators = [{ name: 'sma', window: 20 }];
      service.calculateIndicators('AAPL', '2026-01-01', '2026-01-31', indicators).subscribe();

      const req = httpMock.expectOne(GRAPHQL_URL);
      expect(req.request.body.variables.ticker).toBe('AAPL');
      expect(req.request.body.variables.indicators).toEqual(indicators);
      req.flush({
        data: {
          calculateIndicators: {
            success: true, ticker: 'AAPL',
            indicators: [createMockIndicatorSeries()], message: null,
          },
        },
      });
    });

    it('should map indicator response correctly', async () => {
      const indicators = [{ name: 'sma', window: 20 }];

      const promise = firstValueFrom(
        service.calculateIndicators('AAPL', '2026-01-01', '2026-01-31', indicators)
      );

      httpMock.expectOne(GRAPHQL_URL).flush({
        data: {
          calculateIndicators: {
            success: true, ticker: 'AAPL',
            indicators: [createMockIndicatorSeries()], message: null,
          },
        },
      });

      const result = await promise;
      expect(result.success).toBe(true);
      expect(result.indicators.length).toBe(1);
    });
  });
});
