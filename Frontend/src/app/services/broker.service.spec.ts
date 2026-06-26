import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { BrokerService } from './broker.service';

describe('BrokerService diagnostics endpoints', () => {
  let service: BrokerService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    service = TestBed.inject(BrokerService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    http.verify();
  });

  it('loads data-plane health from the broker diagnostics contract', async () => {
    const promise = service.dataPlaneHealth();
    const req = http.expectOne('/api/broker/data-plane/health');

    expect(req.request.method).toBe('GET');
    req.flush({
      service: 'polygon-data-service',
      code_revision: '8398d285978a94d9714490e002962e365e9cd505',
      process_start_ms: 1_780_000_100_000,
      fetched_at_ms: 1_780_000_200_000,
      reload: 'watchfiles-polling',
    });

    await expect(promise).resolves.toEqual({
      service: 'polygon-data-service',
      code_revision: '8398d285978a94d9714490e002962e365e9cd505',
      process_start_ms: 1_780_000_100_000,
      fetched_at_ms: 1_780_000_200_000,
      reload: 'watchfiles-polling',
    });
  });

  it('loads IBKR API evidence backfill with an explicit cursor and limit', async () => {
    const promise = service.ibkrApiEvidence(7, 120);
    const req = http.expectOne(
      (request) =>
        request.url === '/api/broker/ibkr/evidence' &&
        request.params.get('after_seq') === '7' &&
        request.params.get('limit') === '120',
    );

    expect(req.request.method).toBe('GET');
    req.flush([]);

    await expect(promise).resolves.toEqual([]);
  });
});
