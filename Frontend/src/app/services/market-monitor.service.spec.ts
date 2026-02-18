import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { firstValueFrom } from 'rxjs';
import { MarketMonitorService } from './market-monitor.service';
import {
  MarketStatusResponse,
  MarketHolidaysResponse,
  MarketDashboardResponse,
} from '../models/market-monitor';

describe('MarketMonitorService', () => {
  let service: MarketMonitorService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    service = TestBed.inject(MarketMonitorService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => httpMock.verify());

  describe('getMarketStatus', () => {
    it('should GET from Python service /api/market/status', () => {
      service.getMarketStatus().subscribe();

      const req = httpMock.expectOne('http://localhost:8000/api/market/status');
      expect(req.request.method).toBe('GET');
      req.flush({
        success: true,
        market: 'open',
        exchanges: { nyse: 'open', nasdaq: 'open', otc: 'open' },
        early_hours: false,
        after_hours: false,
        server_time: '2026-02-17T10:00:00Z',
        server_time_readable: 'Feb 17 10:00 AM',
        error: null,
      } satisfies MarketStatusResponse);
    });

    it('should return the response on success', async () => {
      const mockResponse: MarketStatusResponse = {
        success: true,
        market: 'closed',
        exchanges: { nyse: 'closed', nasdaq: 'closed', otc: 'closed' },
        early_hours: false,
        after_hours: false,
        server_time: '2026-02-17T20:00:00Z',
        server_time_readable: 'Feb 17 8:00 PM',
        error: null,
      };

      const promise = firstValueFrom(service.getMarketStatus());

      httpMock.expectOne('http://localhost:8000/api/market/status').flush(mockResponse);

      const result = await promise;
      expect(result.market).toBe('closed');
      expect(result.success).toBe(true);
    });

    it('should throw when success is false', async () => {
      const promise = firstValueFrom(service.getMarketStatus());

      httpMock.expectOne('http://localhost:8000/api/market/status').flush({
        success: false,
        market: 'unknown',
        exchanges: { nyse: null, nasdaq: null, otc: null },
        early_hours: false,
        after_hours: false,
        server_time: '',
        server_time_readable: '',
        error: 'Failed to fetch market status',
      } satisfies MarketStatusResponse);

      await expect(promise).rejects.toThrow('Failed to fetch market status');
    });
  });

  describe('getHolidays', () => {
    it('should GET from Python service /api/market/holidays with limit param', () => {
      service.getHolidays(10).subscribe();

      const req = httpMock.expectOne(
        r => r.url === 'http://localhost:8000/api/market/holidays' && r.params.get('limit') === '10'
      );
      expect(req.request.method).toBe('GET');
      req.flush({
        success: true,
        events: [],
        count: 0,
        error: null,
      } satisfies MarketHolidaysResponse);
    });

    it('should map response to events array', async () => {
      const events = [
        { date: '2026-05-25', name: 'Memorial Day', status: 'Closed', open: null, close: null, exchanges: ['NYSE', 'NASDAQ'] },
      ];

      const promise = firstValueFrom(service.getHolidays());

      httpMock.expectOne(r => r.url === 'http://localhost:8000/api/market/holidays').flush({
        success: true,
        events,
        count: 1,
        error: null,
      } satisfies MarketHolidaysResponse);

      const result = await promise;
      expect(result).toEqual(events);
    });

    it('should cache holidays (shareReplay)', () => {
      service.getHolidays().subscribe();
      service.getHolidays().subscribe();

      // Only one HTTP request should be made due to caching
      const reqs = httpMock.match(r => r.url === 'http://localhost:8000/api/market/holidays');
      expect(reqs.length).toBe(1);
      reqs[0].flush({
        success: true,
        events: [],
        count: 0,
        error: null,
      });
    });

    it('should throw when success is false', async () => {
      const promise = firstValueFrom(service.getHolidays());

      httpMock.expectOne(r => r.url === 'http://localhost:8000/api/market/holidays').flush({
        success: false,
        events: [],
        count: 0,
        error: 'Failed to fetch holidays',
      } satisfies MarketHolidaysResponse);

      await expect(promise).rejects.toThrow('Failed to fetch holidays');
    });
  });

  describe('getDashboard', () => {
    it('should GET from Python service /api/market/dashboard', () => {
      service.getDashboard().subscribe();

      const req = httpMock.expectOne('http://localhost:8000/api/market/dashboard');
      expect(req.request.method).toBe('GET');
      req.flush({
        success: true,
        status: null,
        holidays: null,
        error: null,
      } satisfies MarketDashboardResponse);
    });

    it('should throw when success is false', async () => {
      const promise = firstValueFrom(service.getDashboard());

      httpMock.expectOne('http://localhost:8000/api/market/dashboard').flush({
        success: false,
        status: null,
        holidays: null,
        error: 'Failed to fetch dashboard',
      } satisfies MarketDashboardResponse);

      await expect(promise).rejects.toThrow('Failed to fetch dashboard');
    });
  });

  describe('clearCache', () => {
    it('should allow fresh holiday requests after clearing', () => {
      service.getHolidays().subscribe();
      httpMock.expectOne(r => r.url === 'http://localhost:8000/api/market/holidays').flush({
        success: true, events: [], count: 0, error: null,
      });

      service.clearCache();

      service.getHolidays().subscribe();
      // A new request should be made after clearing
      const req = httpMock.expectOne(r => r.url === 'http://localhost:8000/api/market/holidays');
      req.flush({ success: true, events: [], count: 0, error: null });
    });
  });
});
