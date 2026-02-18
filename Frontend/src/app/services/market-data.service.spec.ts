import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { firstValueFrom } from 'rxjs';
import { MarketDataService } from './market-data.service';
import { createMockAggregate, createMockSummary, createMockIndicatorSeries } from '../../testing/factories/market-data.factory';

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

      const req = httpMock.expectOne('http://localhost:5000/graphql');
      expect(req.request.method).toBe('POST');
      req.flush({ data: { getOrFetchStockAggregates: { ticker: 'AAPL', aggregates: [], summary: null } } });
    });

    it('should send correct variables', () => {
      service.getOrFetchStockAggregates('MSFT', '2026-01-01', '2026-06-30', 'hour', 4).subscribe();

      const req = httpMock.expectOne('http://localhost:5000/graphql');
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

      httpMock.expectOne('http://localhost:5000/graphql').flush({
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

      httpMock.expectOne('http://localhost:5000/graphql').flush({
        data: null,
        errors: [{ message: 'Ticker not found' }],
      });

      await expect(promise).rejects.toThrow('Ticker not found');
    });
  });

  describe('calculateIndicators', () => {
    it('should send correct variables', () => {
      const indicators = [{ name: 'sma', window: 20 }];
      service.calculateIndicators('AAPL', '2026-01-01', '2026-01-31', indicators).subscribe();

      const req = httpMock.expectOne('http://localhost:5000/graphql');
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

      httpMock.expectOne('http://localhost:5000/graphql').flush({
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
