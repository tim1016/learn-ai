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

  it('loads account truth from the account-truth endpoint', async () => {
    const promise = service.accountTruth();
    const req = http.expectOne('/api/broker/account-truth');

    expect(req.request.method).toBe('GET');
    req.flush({
      account_id: 'DU1234567',
      final_verdict: 'clean',
      final_severity: 'ok',
      status_label: 'Clean',
      status_detail: 'Required live broker evidence is assigned to known ownership.',
      generated_at_ms: 1_780_000_000_000,
      health: {},
      account: null,
      known_bot_namespaces: [],
      manual_namespaces_observed: [],
      invariants: [],
      blockers: [],
      operator_blockers: [],
      caveats: [],
      owner_summaries: [],
      symbol_exposures: [],
      orders: [],
      executions: [],
      positions: [],
      evidence_gaps: [],
      source_freshness: [],
    });

    await expect(promise).resolves.toMatchObject({
      account_id: 'DU1234567',
      final_verdict: 'clean',
    });
  });

  it('loads account events with an explicit view, cursor, and repeated kind filters', async () => {
    const promise = service.accountEvents('DU 123', {
      view: 'operations',
      limit: 25,
      kinds: ['safety', 'clerk'],
      beforeSeq: 42,
    });
    const req = http.expectOne(
      (request) =>
        request.url === '/api/accounts/DU%20123/events' &&
        request.params.get('view') === 'operations' &&
        request.params.get('limit') === '25' &&
        request.params.getAll('kinds')?.join(',') === 'safety,clerk' &&
        request.params.get('before_seq') === '42',
    );

    expect(req.request.method).toBe('GET');
    req.flush({
      schema_version: 1,
      account_id: 'DU 123',
      view: 'operations',
      rows: [],
      latest_seq: 41,
      next_before_seq: null,
    });

    await expect(promise).resolves.toMatchObject({ latest_seq: 41 });
  });

  it('loads completed orders from the completed-order endpoint', async () => {
    const promise = service.completedOrders();
    const req = http.expectOne('/api/broker/orders/completed');

    expect(req.request.method).toBe('GET');
    req.flush([]);

    await expect(promise).resolves.toEqual([]);
  });

  it('updates the saved account reconciliation automation policy', async () => {
    const promise = service.updateAccountReconciliationAutomation('DU 123', {
      enabled: true,
    });
    const req = http.expectOne('/api/accounts/DU%20123/reconciliation/automation');

    expect(req.request.method).toBe('PUT');
    expect(req.request.body).toEqual({ enabled: true });
    req.flush({
      schema_version: 1,
      account_id: 'DU 123',
      enabled: true,
      updated_at_ms: 1_780_000_000_000,
      updated_by: 'account-monitor.operator',
    });

    await expect(promise).resolves.toMatchObject({ enabled: true });
  });

  it('posts only the closed presented reconciliation envelope', async () => {
    const action = {
      action_id: 'reconcile_now' as const,
      target: { account_id: 'DU 123' },
      snapshot_id: 'a'.repeat(64),
      snapshot_version: 'b'.repeat(64),
      evidence_refs: [],
      effect_class: 'EVIDENCE_REFRESH' as const,
      idempotency_key: 'c'.repeat(64),
      issued_at_ms: 1_780_000_000_000,
      expires_at_ms: 1_780_000_060_000,
      presentation_token: 'd'.repeat(64),
      preconditions: [],
      confirmation: { title: 'Title', body: 'Body', consequence: 'Consequence', confirm_label: 'Confirm', required_token: '' },
      availability: 'AVAILABLE' as const,
      disposition: 'fix_here' as const,
      finished_copy: 'Finished.',
    };
    const promise = service.executePresentedReconcileNow('DU 123', action);
    const req = http.expectOne('/api/accounts/DU%20123/presented-actions/reconcile-now');

    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({
      action_id: action.action_id,
      target: action.target,
      snapshot_id: action.snapshot_id,
      snapshot_version: action.snapshot_version,
      idempotency_key: action.idempotency_key,
      issued_at_ms: action.issued_at_ms,
      expires_at_ms: action.expires_at_ms,
      presentation_token: action.presentation_token,
    });
    req.flush({
      action_id: 'reconcile_now',
      action_attempt_id: action.idempotency_key,
      state: 'ACCEPTED',
      replayed: false,
      finished_copy: action.finished_copy,
      reconciliation_receipt: null,
      refreshed_snapshot_id: 'd'.repeat(64),
    });

    await expect(promise).resolves.toMatchObject({ state: 'ACCEPTED' });
  });

  it('posts a typed recovery confirmation only with the presented envelope', async () => {
    const action = {
      action_id: 'flatten' as const,
      target: { account_id: 'DU 123', kind: 'ACCOUNT' as const },
      snapshot_id: 'a'.repeat(64),
      snapshot_version: 'b'.repeat(64),
      evidence_refs: [],
      effect_class: 'RISK_REDUCING_BROKER' as const,
      idempotency_key: 'c'.repeat(64),
      issued_at_ms: 1_780_000_000_000,
      expires_at_ms: 1_780_000_060_000,
      presentation_token: 'd'.repeat(64),
      preconditions: [],
      confirmation: { title: 'Title', body: 'Body', consequence: 'Consequence', confirm_label: 'Confirm', required_token: 'FLATTEN' },
      availability: 'AVAILABLE' as const,
      disposition: 'fix_here' as const,
      finished_copy: 'Finished.',
    };
    const promise = service.executePresentedRecoveryAction('DU 123', action, 'FLATTEN');
    const req = http.expectOne('/api/accounts/DU%20123/presented-actions/recovery');

    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({
      action_id: action.action_id,
      target: action.target,
      snapshot_id: action.snapshot_id,
      snapshot_version: action.snapshot_version,
      idempotency_key: action.idempotency_key,
      issued_at_ms: action.issued_at_ms,
      expires_at_ms: action.expires_at_ms,
      presentation_token: action.presentation_token,
      confirmation_token: 'FLATTEN',
    });
    req.flush({
      action_id: 'flatten',
      action_attempt_id: action.idempotency_key,
      state: 'PENDING_PROOF',
      replayed: false,
      finished_copy: 'Flatten intention recorded.',
      effect_receipt: { kind: 'FLATTEN_INTENTION_RECORDED', account_id: 'DU 123', recorded_at_ms: 1_780_000_000_000 },
      reconciliation_receipt: null,
      refreshed_snapshot_id: null,
    });

    await expect(promise).resolves.toMatchObject({ state: 'PENDING_PROOF' });
  });

  it('posts what-if previews to the non-submitting endpoint', async () => {
    const spec = {
      symbol: 'SPY',
      sec_type: 'STK' as const,
      action: 'BUY' as const,
      quantity: 1,
      order_type: 'MKT' as const,
      time_in_force: 'DAY' as const,
      multiplier: 100,
      confirm_paper: false,
      manual_order: true,
    };
    const promise = service.orderWhatIf(spec);
    const req = http.expectOne('/api/broker/orders/what-if');

    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual(spec);
    req.flush({
      account_id: 'DU1234567',
      is_paper: true,
      symbol: 'SPY',
      action: 'BUY',
      quantity: 1,
      order_type: 'MKT',
      init_margin_change: 10,
      maint_margin_change: 5,
      equity_with_loan_change: -10,
      commission: 1,
      warning_text: null,
      order_ref: null,
      previewed_at_ms: 1_780_000_000_000,
    });

    await expect(promise).resolves.toMatchObject({
      init_margin_change: 10,
      commission: 1,
    });
  });
});
