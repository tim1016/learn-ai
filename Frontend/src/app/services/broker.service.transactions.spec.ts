import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { BrokerService } from './broker.service';

describe('BrokerService Clerk transaction history', () => {
  let service: BrokerService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({ providers: [provideHttpClient(), provideHttpClientTesting()] });
    service = TestBed.inject(BrokerService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('relays an opaque history cursor without deriving transaction state', async () => {
    const promise = service.accountTransactions('DU1219', 'ctxhp1.opaque', 25);
    const request = http.expectOne('/api/accounts/DU1219/transactions?limit=25&cursor=ctxhp1.opaque');
    expect(request.request.method).toBe('GET');
    request.flush({ projection_available: true, canonical_fallback_required: false, high_water_journal_seq: 4, lag_records: 0, lag_is_lower_bound: false, rows: [], next_cursor: null });
    await expect(promise).resolves.toMatchObject({ high_water_journal_seq: 4, rows: [] });
  });

  it('sends account-scoped filters to the bounded projection endpoint', async () => {
    const promise = service.accountTransactions('DU1219', null, 25, {
      origin: 'strategy',
      lifecycleState: 'partially_filled',
      strategyInstanceId: 'bot-1',
      runId: 'run-1',
    });
    const request = http.expectOne(
      '/api/accounts/DU1219/transactions?limit=25&origin=strategy&lifecycle_state=partially_filled&strategy_instance_id=bot-1&run_id=run-1',
    );
    expect(request.request.method).toBe('GET');
    request.flush({ projection_available: true, canonical_fallback_required: false, high_water_journal_seq: 4, lag_records: 0, lag_is_lower_bound: false, rows: [], next_cursor: null });
    await expect(promise).resolves.toMatchObject({ rows: [] });
  });

  it('sends an inclusive UTC-millisecond history window without browser-side filtering', async () => {
    const promise = service.accountTransactions('DU1219', null, 25, {
      fromMs: 1_700_000_000_000,
      toMs: 1_700_086_400_000,
    });
    const request = http.expectOne(
      '/api/accounts/DU1219/transactions?limit=25&from_ms=1700000000000&to_ms=1700086400000',
    );
    expect(request.request.method).toBe('GET');
    request.flush({ projection_available: true, canonical_fallback_required: false, high_water_journal_seq: 4, lag_records: 0, lag_is_lower_bound: false, rows: [], next_cursor: null });
    await expect(promise).resolves.toMatchObject({ rows: [] });
  });
});
