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
});
