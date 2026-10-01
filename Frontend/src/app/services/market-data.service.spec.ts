import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { firstValueFrom } from 'rxjs';
import { MarketDataService } from './market-data.service';
import { createMockAggregate } from '../../testing/factories/market-data.factory';
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
        adjusted: true,
      });
      req.flush({ data: { getOrFetchStockAggregates: { ticker: 'MSFT', aggregates: [] } } });
    });

    it('should map response to SmartAggregatesResult', async () => {
      const aggregate = createMockAggregate();

      const promise = firstValueFrom(
        service.getOrFetchStockAggregates('AAPL', '2026-01-01', '2026-01-31')
      );

      httpMock.expectOne(GRAPHQL_URL).flush({
        data: { getOrFetchStockAggregates: { ticker: 'AAPL', aggregates: [aggregate] } },
      });

      const result = await promise;
      expect(result.ticker).toBe('AAPL');
      expect(result.aggregates.length).toBe(1);
      expect(result.aggregates[0].open).toBe(aggregate.open);
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

  // Python owns the default rate: an unset rate travels as null, so the
  // Backend omits it and Python fills its one default (#2764).
  describe('risk-free rate', () => {
    it('analyzeOptionsStrategy sends no rate unless given one', () => {
      service.analyzeOptionsStrategy('SPY', [], '2099-01-01', 100).subscribe();

      const req = httpMock.expectOne(GRAPHQL_URL);
      expect(req.request.body.variables.riskFreeRate).toBeNull();
      req.flush({ data: { analyzeOptionsStrategy: {} } });
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
});
