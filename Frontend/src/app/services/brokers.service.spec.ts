import { provideHttpClient } from '@angular/common/http';
import {
  HttpTestingController,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { BrokersService } from './brokers.service';

describe('BrokersService', () => {
  let service: BrokersService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting(), BrokersService],
    });
    service = TestBed.inject(BrokersService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => httpMock.verify());

  it('GETs the account for the named broker', async () => {
    const promise = service.getAccount('alpaca');

    const req = httpMock.expectOne('/api/brokers/alpaca/account');
    expect(req.request.method).toBe('GET');
    req.flush({ account_id: 'PA1' });

    await expect(promise).resolves.toMatchObject({ account_id: 'PA1' });
  });

  it('defaults the broker to alpaca', async () => {
    const promise = service.getAccount();

    httpMock.expectOne('/api/brokers/alpaca/account').flush({ account_id: 'PA1' });

    await promise;
  });

  it('POSTs an order to the control-prefixed orders endpoint', async () => {
    const request = {
      operator: 'desk',
      legs: [{ symbol: 'SPY', side: 'buy' as const, quantity: 1, order_type: 'market' as const }],
    };
    const promise = service.submitOrder('alpaca', request);

    const req = httpMock.expectOne('/api/brokers/alpaca/orders');
    expect(req.request.method).toBe('POST');
    // The URL is under /api/brokers — a registered data-plane control prefix —
    // so the control-intent interceptor marks it for the proxy to authenticate.
    expect(req.request.url).toBe('/api/brokers/alpaca/orders');
    expect(req.request.body).toEqual(request);
    req.flush({ broker: 'alpaca', account_id: 'PA1', results: [] });

    await expect(promise).resolves.toMatchObject({ account_id: 'PA1' });
  });

  it('DELETEs an order by its broker id on the control-prefixed endpoint', async () => {
    const promise = service.cancelOrder('alpaca', 'broker-order-xyz');

    const req = httpMock.expectOne('/api/brokers/alpaca/orders/broker-order-xyz');
    expect(req.request.method).toBe('DELETE');
    // DELETE under /api/brokers is a control mutation — the interceptor marks it.
    req.flush({
      broker: 'alpaca',
      account_id: 'PA1',
      order_id: 'broker-order-xyz',
      status: 'acked',
      owned: true,
      order_ref: 'manual/desk/v1:abc',
      error: null,
    });

    await expect(promise).resolves.toMatchObject({ status: 'acked', owned: true });
  });

  it('GETs the clerk status for the named broker', async () => {
    const promise = service.getClerkStatus('alpaca');

    const req = httpMock.expectOne('/api/brokers/alpaca/clerk/status');
    expect(req.request.method).toBe('GET');
    req.flush({
      broker: 'alpaca',
      account_id: 'PA1',
      hold: { active: false, reason_code: null, reason: null, since_ms: null },
      latest_reconciliation: null,
      outstanding_intents: 0,
      observed_at_ms: 1,
    });

    await expect(promise).resolves.toMatchObject({ hold: { active: false } });
  });

  it('POSTs clear-hold to the control-prefixed endpoint', async () => {
    const promise = service.clearHold('alpaca', { operator: 'ops', reason: 'safe' });

    const req = httpMock.expectOne('/api/brokers/alpaca/clerk/clear-hold');
    expect(req.request.method).toBe('POST');
    // POST under /api/brokers is a control mutation — the interceptor marks it.
    expect(req.request.body).toEqual({ operator: 'ops', reason: 'safe' });
    req.flush({
      broker: 'alpaca',
      account_id: 'PA1',
      hold: { active: false, reason_code: null, reason: null, since_ms: null },
      latest_reconciliation: null,
      outstanding_intents: 0,
      observed_at_ms: 2,
    });

    await expect(promise).resolves.toMatchObject({ hold: { active: false } });
  });
});
