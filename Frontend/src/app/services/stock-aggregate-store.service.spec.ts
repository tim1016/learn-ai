import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { StockAggregateStore } from './stock-aggregate-store.service';
import { MarketDataService } from './market-data.service';
import { createMockAggregate } from '../../testing/factories/market-data.factory';

describe('StockAggregateStore', () => {
  let store: StockAggregateStore;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    store = TestBed.inject(StockAggregateStore);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => httpMock.verify());

  describe('getCacheStats', () => {
    it('should start empty', () => {
      const stats = store.getCacheStats();
      expect(stats.size).toBe(0);
      expect(stats.entries).toBe(0);
    });
  });

  describe('clearCache', () => {
    it('should clear all cached entries', () => {
      // Manually populate cache via getOrFetch
      const promise = store.getOrFetch('AAPL', '2026-01-01', '2026-01-31');
      httpMock.expectOne('http://localhost:5000/graphql').flush({
        data: {
          getOrFetchStockAggregates: {
            ticker: 'AAPL',
            aggregates: [createMockAggregate()],
            summary: null,
          },
        },
      });

      promise.then(() => {
        expect(store.getCacheStats().size).toBe(1);

        store.clearCache();
        expect(store.getCacheStats().size).toBe(0);
      });
    });
  });

  describe('invalidateTicker', () => {
    it('should remove only the specified ticker from cache', async () => {
      // Cache AAPL
      const p1 = store.getOrFetch('AAPL', '2026-01-01', '2026-01-31');
      httpMock.expectOne('http://localhost:5000/graphql').flush({
        data: {
          getOrFetchStockAggregates: {
            ticker: 'AAPL',
            aggregates: [createMockAggregate()],
            summary: null,
          },
        },
      });
      await p1;

      // Cache MSFT
      const p2 = store.getOrFetch('MSFT', '2026-01-01', '2026-01-31');
      httpMock.expectOne('http://localhost:5000/graphql').flush({
        data: {
          getOrFetchStockAggregates: {
            ticker: 'MSFT',
            aggregates: [createMockAggregate({ id: 2 })],
            summary: null,
          },
        },
      });
      await p2;

      expect(store.getCacheStats().size).toBe(2);

      store.invalidateTicker('AAPL');
      expect(store.getCacheStats().size).toBe(1);
    });
  });

  describe('getOrFetch', () => {
    it('should fetch from MarketDataService when cache is empty', async () => {
      const aggregate = createMockAggregate();
      const promise = store.getOrFetch('AAPL', '2026-01-01', '2026-01-31');

      const req = httpMock.expectOne('http://localhost:5000/graphql');
      expect(req.request.method).toBe('POST');
      req.flush({
        data: {
          getOrFetchStockAggregates: {
            ticker: 'AAPL',
            aggregates: [aggregate],
            summary: null,
          },
        },
      });

      const result = await promise;
      expect(result.aggregates.length).toBe(1);
    });

    it('should return cached data on second call', async () => {
      const aggregate = createMockAggregate();

      // First call — fetches from API
      const p1 = store.getOrFetch('AAPL', '2026-01-01', '2026-01-31');
      httpMock.expectOne('http://localhost:5000/graphql').flush({
        data: {
          getOrFetchStockAggregates: {
            ticker: 'AAPL',
            aggregates: [aggregate],
            summary: null,
          },
        },
      });
      await p1;

      // Second call — should use cache (no HTTP request)
      const result = await store.getOrFetch('AAPL', '2026-01-01', '2026-01-31');
      expect(result.aggregates.length).toBe(1);
      httpMock.expectNone('http://localhost:5000/graphql');
    });

    it('should force refresh when requested', async () => {
      const aggregate = createMockAggregate();

      // Populate cache
      const p1 = store.getOrFetch('AAPL', '2026-01-01', '2026-01-31');
      httpMock.expectOne('http://localhost:5000/graphql').flush({
        data: {
          getOrFetchStockAggregates: {
            ticker: 'AAPL',
            aggregates: [aggregate],
            summary: null,
          },
        },
      });
      await p1;

      // Force refresh — should make a new HTTP call
      const p2 = store.getOrFetch('AAPL', '2026-01-01', '2026-01-31', 'day', 1, true);
      httpMock.expectOne('http://localhost:5000/graphql').flush({
        data: {
          getOrFetchStockAggregates: {
            ticker: 'AAPL',
            aggregates: [createMockAggregate({ close: 999 })],
            summary: null,
          },
        },
      });
      const result = await p2;
      expect(result.aggregates[0].close).toBe(999);
    });
  });

  describe('getAggregatesByTicker', () => {
    it('should return empty map when no data cached', () => {
      const map = store.getAggregatesByTicker();
      expect(map.size).toBe(0);
    });

    it('should return ticker data after fetching', async () => {
      const aggregate = createMockAggregate();
      const promise = store.getOrFetch('AAPL', '2026-01-01', '2026-01-31');
      httpMock.expectOne('http://localhost:5000/graphql').flush({
        data: {
          getOrFetchStockAggregates: {
            ticker: 'AAPL',
            aggregates: [aggregate],
            summary: null,
          },
        },
      });
      await promise;

      const map = store.getAggregatesByTicker();
      expect(map.has('AAPL')).toBe(true);
      expect(map.get('AAPL')!.length).toBe(1);
    });
  });
});
