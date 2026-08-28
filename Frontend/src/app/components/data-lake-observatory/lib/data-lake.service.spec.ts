import { HttpErrorResponse, provideHttpClient } from '@angular/common/http';
import {
  HttpTestingController,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { beforeEach, describe, expect, it } from 'vitest';

import { environment } from '../../../../environments/environment';
import { DataLakeService, classifyDataLakeError } from './data-lake.service';

const BASE = `${environment.pythonServiceUrl}/api/data-lake`;

describe('DataLakeService', () => {
  let service: DataLakeService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    service = TestBed.inject(DataLakeService);
    http = TestBed.inject(HttpTestingController);
  });

  it('sends the coverage window as the endpoint declares it', async () => {
    const pending = service.coverage({
      symbol: 'SPY',
      startTradingDate: '2026-05-18',
      endTradingDate: '2026-05-20',
      dataType: 'quote',
      priceAdjustmentMode: 'lean_adjusted',
    });

    const request = http.expectOne((candidate) => candidate.url === `${BASE}/coverage`);
    expect(request.request.params.get('symbol')).toBe('SPY');
    expect(request.request.params.get('start_trading_date')).toBe('2026-05-18');
    expect(request.request.params.get('end_trading_date')).toBe('2026-05-20');
    expect(request.request.params.get('data_type')).toBe('quote');
    expect(request.request.params.get('price_adjustment_mode')).toBe('lean_adjusted');
    expect(request.request.params.get('market')).toBe('usa');

    request.flush({ market: 'usa', symbol: 'SPY', days: [] });
    await expect(pending).resolves.toMatchObject({ kind: 'ok' });
    http.verify();
  });

  it('reports a 404 as "not enabled" rather than an unclassified failure', async () => {
    const pending = service.storageSummary();

    http
      .expectOne((candidate) => candidate.url === `${BASE}/storage-summary`)
      .flush({ detail: 'Not Found' }, { status: 404, statusText: 'Not Found' });

    await expect(pending).resolves.toEqual({ kind: 'not_enabled' });
    http.verify();
  });

  it("keeps the endpoint's own reason code on a 422", async () => {
    const pending = service.coverage({
      symbol: 'SPY',
      startTradingDate: '2020-01-01',
      endTradingDate: '2030-01-01',
    });

    http.expectOne((candidate) => candidate.url === `${BASE}/coverage`).flush(
      { detail: { reason: 'range_too_large', message: 'range is 3654 days; max is 1830' } },
      { status: 422, statusText: 'Unprocessable Entity' },
    );

    await expect(pending).resolves.toEqual({
      kind: 'rejected',
      reason: 'range_too_large',
      message: 'range is 3654 days; max is 1830',
    });
    http.verify();
  });
});

describe('classifyDataLakeError', () => {
  it('names an unreachable data plane instead of surfacing a bare status 0', () => {
    const result = classifyDataLakeError(
      new HttpErrorResponse({ status: 0, statusText: 'Unknown Error' }),
    );

    expect(result).toEqual({ kind: 'unavailable', message: 'The data plane did not respond.' });
  });

  it('falls back to validation_failed when a 422 body carries no reason', () => {
    const result = classifyDataLakeError(
      new HttpErrorResponse({ status: 422, statusText: 'Unprocessable Entity', error: {} }),
    );

    expect(result).toMatchObject({ kind: 'rejected', reason: 'validation_failed' });
  });

  it('does not pretend a non-HTTP throw was an HTTP answer', () => {
    expect(classifyDataLakeError(new Error('boom'))).toEqual({
      kind: 'unavailable',
      message: 'boom',
    });
  });
});
