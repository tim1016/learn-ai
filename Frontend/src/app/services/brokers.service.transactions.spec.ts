import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { BrokersService } from './brokers.service';

describe('BrokersService Clerk transaction history', () => {
  let service: BrokersService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({ providers: [provideHttpClient(), provideHttpClientTesting()] });
    service = TestBed.inject(BrokersService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('relays an opaque history cursor without deriving transaction state', async () => {
    const promise = service.accountTransactions('PA / 1', 'ctxhp1.opaque', 25, {});
    const request = http.expectOne('/api/accounts/PA%20%2F%201/transactions?limit=25&cursor=ctxhp1.opaque');
    expect(request.request.method).toBe('GET');
    request.flush({ projection_available: true, canonical_fallback_required: false, high_water_journal_seq: 4, lag_records: 0, lag_is_lower_bound: false, rows: [], next_cursor: null });
    await expect(promise).resolves.toMatchObject({ high_water_journal_seq: 4, rows: [] });
  });

  it('sends account-scoped filters to the bounded projection endpoint', async () => {
    const promise = service.accountTransactions('PA / 1', null, 25, {
      origin: 'strategy',
      lifecycleState: 'partially_filled',
      strategyInstanceId: 'bot-1',
      runId: 'run-1',
    });
    const request = http.expectOne(
      '/api/accounts/PA%20%2F%201/transactions?limit=25&origin=strategy&lifecycle_state=partially_filled&strategy_instance_id=bot-1&run_id=run-1',
    );
    expect(request.request.method).toBe('GET');
    request.flush({ projection_available: true, canonical_fallback_required: false, high_water_journal_seq: 4, lag_records: 0, lag_is_lower_bound: false, rows: [], next_cursor: null });
    await expect(promise).resolves.toMatchObject({ rows: [] });
  });

  it('sends an inclusive UTC-millisecond history window without browser-side filtering', async () => {
    const promise = service.accountTransactions('PA / 1', null, 25, {
      fromMs: 1_700_000_000_000,
      toMs: 1_700_086_400_000,
    });
    const request = http.expectOne(
      '/api/accounts/PA%20%2F%201/transactions?limit=25&from_ms=1700000000000&to_ms=1700086400000',
    );
    expect(request.request.method).toBe('GET');
    request.flush({ projection_available: true, canonical_fallback_required: false, high_water_journal_seq: 4, lag_records: 0, lag_is_lower_bound: false, rows: [], next_cursor: null });
    await expect(promise).resolves.toMatchObject({ rows: [] });
  });

  it('GETs the canonical SQLite transaction projection, never generic broker orders', async () => {
    const promise = service.accountTransactions('PA / 1', null, 25, {
      origin: 'strategy',
      lifecycleState: 'filled',
      strategyInstanceId: 'bot-1',
      runId: 'run-1',
    });

    const req = http.expectOne(
      (request) =>
        request.url === '/api/accounts/PA%20%2F%201/transactions'
        && request.params.get('limit') === '25'
        && request.params.get('origin') === 'strategy'
        && request.params.get('lifecycle_state') === 'filled'
        && request.params.get('strategy_instance_id') === 'bot-1'
        && request.params.get('run_id') === 'run-1',
    );
    expect(req.request.method).toBe('GET');
    http.expectNone('/api/brokers/alpaca/orders');
    req.flush({
      projection_available: true,
      canonical_fallback_required: false,
      feed_state: 'live',
      feed_headline: 'SQLite projection current',
      feed_detail: 'One durable execution receipt is available.',
      high_water_journal_seq: 12,
      lag_records: 0,
      lag_is_lower_bound: false,
      custody_summary: {
        record_count: 1,
        a0_custody_accepted_count: 1,
        a1_broker_write_started_count: 1,
        a2_broker_known_count: 1,
        a3_economic_terminal_count: 1,
        uncertain_count: 0,
      },
      rows: [
        {
          transaction_id: 'txn-1',
          broker: 'alpaca',
          account_id: 'PA / 1',
          journal_seq: 12,
          recorded_at_ms: 1_780_000_000_000,
          transaction_kind: 'strategy_execution',
          transaction_origin: 'strategy',
          strategy_instance_id: 'bot-1',
          run_id: 'run-1',
          intent_id: 'intent-1',
          order_ref: 'learn-ai/bot-1/v1:intent-1',
          order_id: null,
          perm_id: null,
          exec_id: null,
          native_order_id: 'alpaca-order-1',
          native_execution_id: 'execution-1',
          lifecycle_state: 'filled',
          commission_status: 'unknown',
          fee: null,
          fee_fidelity: 'not_reported',
          execution_quantity: 2,
          execution_price: 500.25,
          external_order_id: null,
          event_count: 1,
          order_instruction: {
            symbol: 'SPY',
            sec_type: 'us_equity',
            action: 'buy',
            quantity: 2,
            order_type: 'market',
            limit_price: null,
            time_in_force: 'day',
            outside_rth: false,
          },
        },
      ],
      next_cursor: null,
    });

    await expect(promise).resolves.toMatchObject({
      rows: [
        {
          transaction_id: 'txn-1',
          transaction_origin: 'strategy',
          execution_quantity: 2,
          execution_price: 500.25,
        },
      ],
    });
  });
});
