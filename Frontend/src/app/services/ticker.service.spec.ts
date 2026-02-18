import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { firstValueFrom } from 'rxjs';
import { TickerService } from './ticker.service';
import { createMockTicker } from '../../testing/factories/market-data.factory';

describe('TickerService', () => {
  let service: TickerService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    service = TestBed.inject(TickerService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => httpMock.verify());

  describe('getTickers', () => {
    it('should send GraphQL query and map response', async () => {
      const tickers = [createMockTicker({ symbol: 'AAPL' }), createMockTicker({ symbol: 'MSFT', id: 2 })];

      const promise = firstValueFrom(service.getTickers());

      httpMock.expectOne('http://localhost:5000/graphql').flush({
        data: { tickers },
      });

      const result = await promise;
      expect(result.length).toBe(2);
      expect(result[0].symbol).toBe('AAPL');
      expect(result[1].symbol).toBe('MSFT');
    });

    it('should throw on GraphQL errors', async () => {
      const promise = firstValueFrom(service.getTickers());

      httpMock.expectOne('http://localhost:5000/graphql').flush({
        data: null,
        errors: [{ message: 'Query failed' }],
      });

      await expect(promise).rejects.toThrow('Query failed');
    });
  });

  describe('getAggregateStats', () => {
    it('should send symbol variable', () => {
      service.getAggregateStats('AAPL').subscribe();

      const req = httpMock.expectOne('http://localhost:5000/graphql');
      expect(req.request.body.variables).toEqual({ symbol: 'AAPL' });
      req.flush({ data: { stockAggregates: [] } });
    });

    it('should return count and date range from response', async () => {
      const promise = firstValueFrom(service.getAggregateStats('AAPL'));

      httpMock.expectOne('http://localhost:5000/graphql').flush({
        data: {
          stockAggregates: [
            { timestamp: '2026-01-01T00:00:00Z' },
            { timestamp: '2026-01-02T00:00:00Z' },
            { timestamp: '2026-01-03T00:00:00Z' },
          ],
        },
      });

      const stats = await promise;
      expect(stats.count).toBe(3);
      expect(stats.earliest).toBe('2026-01-01T00:00:00Z');
      expect(stats.latest).toBe('2026-01-03T00:00:00Z');
    });

    it('should return nulls for empty response', async () => {
      const promise = firstValueFrom(service.getAggregateStats('UNKNOWN'));

      httpMock.expectOne('http://localhost:5000/graphql').flush({
        data: { stockAggregates: [] },
      });

      const stats = await promise;
      expect(stats.count).toBe(0);
      expect(stats.earliest).toBeNull();
      expect(stats.latest).toBeNull();
    });
  });
});
