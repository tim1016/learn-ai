import { provideHttpClient } from '@angular/common/http';
import {
  HttpTestingController,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import type { SqliteRecoveryAction } from '../api/alpaca.types';
import { resourceTarget } from '../fleet/resource-target';
import { BrokersService } from './brokers.service';
import { TEST_CLERK_ID } from '../fleet/fleet-directory-testing';

const TARGET = resourceTarget('alpaca', TEST_CLERK_ID, {
  accountId: 'PA1',
  routingEpoch: 7,
  bindingGeneration: 3,
});

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

  it('sends the operator proposed price and override in the safe-flatten check body', async () => {
    const promise = service.checkSqliteSafeFlatten(TEST_CLERK_ID, 'PA1', {
      action_id: 'prepare_safe_flatten', concurrency_token: 'reviewed-token', proposed_limit_price: 99.37, band_override: true,
    });
    const request = httpMock.expectOne((req) => req.method === 'POST' && req.url.includes('/recovery-actions/check'));
    expect(request.request.body).toEqual({
      action_id: 'prepare_safe_flatten', concurrency_token: 'reviewed-token', proposed_limit_price: 99.37, band_override: true,
    });
    request.flush({ allowed: true });
    await promise;
  });

  it('GETs the account for the named broker', async () => {
    const promise = service.getAccount(TARGET);

    const req = httpMock.expectOne(`/api/brokers/alpaca/clerks/${TEST_CLERK_ID}/account`);
    expect(req.request.method).toBe('GET');
    req.flush({ account_id: 'PA1' });

    await expect(promise).resolves.toMatchObject({ account_id: 'PA1' });
  });

  it('uses the target broker and clerk identity', async () => {
    const promise = service.getAccount(TARGET);

    httpMock.expectOne(`/api/brokers/alpaca/clerks/${TEST_CLERK_ID}/account`).flush({ account_id: 'PA1' });

    await promise;
  });

  it('GETs account activity with a bounded int64-ms cursor', async () => {
    const promise = service.listActivities(TARGET, {
      afterMs: 1_700_000_000_000,
      limit: 25,
    });

    const req = httpMock.expectOne(
      (request) =>
        request.url === `/api/brokers/alpaca/clerks/${TEST_CLERK_ID}/activities` &&
        request.params.get('after_ms') === '1700000000000' &&
        request.params.get('limit') === '25',
    );
    expect(req.request.method).toBe('GET');
    req.flush([]);

    await expect(promise).resolves.toEqual([]);
  });

  it('requests the backend-owned current trading session', async () => {
    const promise = service.listActivities(TARGET, {
      currentSession: true,
      limit: 100,
    });

    const req = httpMock.expectOne(
      (request) =>
        request.url === `/api/brokers/alpaca/clerks/${TEST_CLERK_ID}/activities` &&
        request.params.get('current_session') === 'true' &&
        request.params.get('limit') === '100' &&
        !request.params.has('after_ms'),
    );
    req.flush([]);

    await expect(promise).resolves.toEqual([]);
  });

  it('coalesces concurrent account reads for the same broker', async () => {
    const first = service.getAccount(TARGET);
    const second = service.getAccount(TARGET);

    const requests = httpMock.match(`/api/brokers/alpaca/clerks/${TEST_CLERK_ID}/account`);
    expect(requests).toHaveLength(1);
    requests[0].flush({ account_id: 'PA-SHARED' });

    await expect(Promise.all([first, second])).resolves.toEqual([
      { account_id: 'PA-SHARED' },
      { account_id: 'PA-SHARED' },
    ]);
  });

  it('keeps generic broker orders available only for diagnostics', async () => {
    const promise = service.listOrders(TEST_CLERK_ID, { status: 'all', limit: 50 });

    const req = httpMock.expectOne(
      (request) =>
        request.url === `/api/brokers/alpaca/clerks/${TEST_CLERK_ID}/orders` &&
        request.params.get('status') === 'all' &&
        request.params.get('limit') === '50',
    );
    expect(req.request.method).toBe('GET');
    req.flush([]);

    await expect(promise).resolves.toEqual([]);
  });

  it('GETs the clerk status for the named broker', async () => {
    const promise = service.getClerkStatus(TARGET);

    const req = httpMock.expectOne(`/api/brokers/alpaca/clerks/${TEST_CLERK_ID}/clerk/status`);
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

  it('GETs the lane-scoped live verdict via the catalog-declared operation', async () => {
    const [promise] = service.getLiveVerdicts([TARGET]);

    const req = httpMock.expectOne(`/api/brokers/alpaca/clerks/${TEST_CLERK_ID}/live-verdict`);
    expect(req.request.method).toBe('GET');
    req.flush({ final_verdict: 'paper' });

    await expect(promise).resolves.toMatchObject({ final_verdict: 'paper' });
  });

  it('issues every lane\'s live verdict at once, and settles each on its own', async () => {
    const other = resourceTarget('alpaca', 'clrk_other');

    const [first, second] = service.getLiveVerdicts([TARGET, other]);

    // Both are in flight together: serialized, the second URL would not exist
    // yet and would inherit the first lane's latency (FR-093).
    httpMock.expectOne(`/api/brokers/alpaca/clerks/${TEST_CLERK_ID}/live-verdict`).flush({
      final_verdict: 'paper',
    });
    httpMock
      .expectOne('/api/brokers/alpaca/clerks/clrk_other/live-verdict')
      .flush('boom', { status: 500, statusText: 'Server Error' });

    await expect(first).resolves.toMatchObject({ final_verdict: 'paper' });
    await expect(second).rejects.toBeDefined();
  });

  it('GETs the custody diagnosis for the named broker', async () => {
    const promise = service.getCustodyDiagnosis(TARGET);

    const req = httpMock.expectOne(`/api/brokers/alpaca/clerks/${TEST_CLERK_ID}/clerk/custody-diagnosis`);
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
    const snapshotPromise = service.getSqliteClerkProjection(TEST_CLERK_ID, 'PA / 1');
    const snapshot = httpMock.expectOne(`/api/brokers/alpaca/clerks/${TEST_CLERK_ID}/accounts/PA%20%2F%201/custody/snapshot`);
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
    const checkPromise = service.checkSqliteRecoveryAction(TEST_CLERK_ID, 'PA1', action, 'bot / 1');
    const check = httpMock.expectOne(
      `/api/brokers/alpaca/clerks/${TEST_CLERK_ID}/accounts/PA1/custody/bots/bot%20%2F%201/recovery-actions/check`,
    );
    expect(check.request.method).toBe('POST');
    expect(check.request.body).toEqual({
      action_id: 'reconcile_now',
      concurrency_token: 'token-1',
    });
    check.flush({ capability: action });
    await expect(checkPromise).resolves.toEqual(action);

    const executePromise = service.executeSqliteRecoveryAction(
      resourceTarget('alpaca', TEST_CLERK_ID, {
        accountId: 'PA1',
        capability: 'custody_command',
        idempotencyKey: 'recovery-1',
        bindingGeneration: 7,
        routingEpoch: 4,
      }),
      action,
    );
    const execute = httpMock.expectOne(
      `/api/brokers/alpaca/clerks/${TEST_CLERK_ID}/accounts/PA1/custody/recovery-actions/execute`,
    );
    expect(execute.request.method).toBe('POST');
    expect(execute.request.body).toEqual({
      action_id: 'reconcile_now',
      concurrency_token: 'token-1',
      execution_ref: null,
      command_context: {
        capability: 'custody_command',
        idempotency_key: 'recovery-1',
        expected_effective_binding_generation: 7,
        target: { account_id: 'PA1' },
      },
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
