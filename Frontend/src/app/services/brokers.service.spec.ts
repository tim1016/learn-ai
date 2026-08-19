import { provideHttpClient } from '@angular/common/http';
import {
  HttpTestingController,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import type { SqliteRecoveryAction } from '../api/alpaca.types';
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

  it('GETs account activity with a bounded int64-ms cursor', async () => {
    const promise = service.listActivities('alpaca', {
      afterMs: 1_700_000_000_000,
      limit: 25,
    });

    const req = httpMock.expectOne(
      (request) =>
        request.url === '/api/brokers/alpaca/activities' &&
        request.params.get('after_ms') === '1700000000000' &&
        request.params.get('limit') === '25',
    );
    expect(req.request.method).toBe('GET');
    req.flush([]);

    await expect(promise).resolves.toEqual([]);
  });

  it('requests the backend-owned current trading session', async () => {
    const promise = service.listActivities('alpaca', {
      currentSession: true,
      limit: 100,
    });

    const req = httpMock.expectOne(
      (request) =>
        request.url === '/api/brokers/alpaca/activities' &&
        request.params.get('current_session') === 'true' &&
        request.params.get('limit') === '100' &&
        !request.params.has('after_ms'),
    );
    req.flush([]);

    await expect(promise).resolves.toEqual([]);
  });

  it('coalesces concurrent account reads for the same broker', async () => {
    const first = service.getAccount('alpaca');
    const second = service.getAccount('alpaca');

    const requests = httpMock.match('/api/brokers/alpaca/account');
    expect(requests).toHaveLength(1);
    requests[0].flush({ account_id: 'PA-SHARED' });

    await expect(Promise.all([first, second])).resolves.toEqual([
      { account_id: 'PA-SHARED' },
      { account_id: 'PA-SHARED' },
    ]);
  });

  it('GETs symbol-grouped orders with the requested filters', async () => {
    const promise = service.listOrderGroups('alpaca', { status: 'all', limit: 50 });

    const req = httpMock.expectOne(
      (request) =>
        request.url === '/api/brokers/alpaca/order-groups' &&
        request.params.get('status') === 'all' &&
        request.params.get('limit') === '50',
    );
    expect(req.request.method).toBe('GET');
    req.flush([]);

    await expect(promise).resolves.toEqual([]);
  });

  it('keeps generic broker orders available only for diagnostics', async () => {
    const promise = service.listOrders('alpaca', { status: 'all', limit: 50 });

    const req = httpMock.expectOne(
      (request) =>
        request.url === '/api/brokers/alpaca/orders' &&
        request.params.get('status') === 'all' &&
        request.params.get('limit') === '50',
    );
    expect(req.request.method).toBe('GET');
    req.flush([]);

    await expect(promise).resolves.toEqual([]);
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

  it('GETs the custody diagnosis for the named broker', async () => {
    const promise = service.getCustodyDiagnosis('alpaca');

    const req = httpMock.expectOne('/api/brokers/alpaca/clerk/custody-diagnosis');
    expect(req.request.method).toBe('GET');
    req.flush({
      broker: 'alpaca',
      account_id: 'PA1',
      in_sync: true,
      observed_at_ms: 1,
      snapshot_version: 'x',
      resolution_posture: 'paper',
      resolvable: false,
      divergences: [],
      resolution_plan: [],
    });

    await expect(promise).resolves.toMatchObject({ in_sync: true });
  });

  it('uses the SQLite snapshot and evidence-bound recovery endpoints', async () => {
    const snapshotPromise = service.getSqliteClerkProjection('PA / 1');
    const snapshot = httpMock.expectOne('/api/alpaca-clerk-sqlite/accounts/PA%20%2F%201/snapshot');
    expect(snapshot.request.method).toBe('GET');
    snapshot.flush({ account_id: 'PA / 1', recovery_actions: [] });
    await snapshotPromise;

    const action = {
      action_id: 'reconcile_now',
      label: 'Reconcile now',
      explanation: 'Compare custody with Alpaca.',
      scope: 'ACCOUNT_CLERK',
      mutation: true,
      available: true,
      unavailable_reason_code: null,
      unavailable_reason: null,
      freshness: 'not_required',
      evidence: [],
      reduction_plan: null,
      next_step: 'Run reconciliation.',
      concurrency_token: 'token-1',
      execution_ref: null,
      confirmation: null,
      primary: true,
    } satisfies SqliteRecoveryAction;
    const checkPromise = service.checkSqliteRecoveryAction('PA1', action, 'bot / 1');
    const check = httpMock.expectOne(
      '/api/alpaca-clerk-sqlite/accounts/PA1/bots/bot%20%2F%201/recovery-actions/check',
    );
    expect(check.request.method).toBe('POST');
    expect(check.request.body).toEqual({
      action_id: 'reconcile_now',
      concurrency_token: 'token-1',
    });
    check.flush({ capability: action });
    await expect(checkPromise).resolves.toEqual(action);

    const executePromise = service.executeSqliteRecoveryAction('PA1', action);
    const execute = httpMock.expectOne(
      '/api/alpaca-clerk-sqlite/accounts/PA1/recovery-actions/execute',
    );
    expect(execute.request.method).toBe('POST');
    expect(execute.request.body).toEqual({
      action_id: 'reconcile_now',
      concurrency_token: 'token-1',
      execution_ref: null,
    });
    execute.flush({
      action_id: 'reconcile_now',
      outcome: 'success',
      applied: true,
      receipt_id: 'reconciliation:12',
      recorded_at_ms: 12,
      command: null,
      reconciliation: {
        verdict: 'clean',
        resolved_count: 0,
        foreign_order_count: 0,
        drifted_symbols: [],
      },
      orders: [],
    });

    await expect(executePromise).resolves.toMatchObject({ applied: true });
  });
});
