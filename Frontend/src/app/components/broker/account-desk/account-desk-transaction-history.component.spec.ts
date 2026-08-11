import { signal } from '@angular/core';
import { fireEvent, render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { AccountDeskTransactionHistoryStore } from './account-desk-transaction-history-store.service';
import { AccountDeskTransactionHistoryComponent } from './account-desk-transaction-history.component';
import { BrokerService } from '../../../services/broker.service';

if (typeof HTMLDialogElement.prototype.showModal !== 'function') {
  HTMLDialogElement.prototype.showModal = function (this: HTMLDialogElement) {
    this.setAttribute('open', '');
  };
}
if (typeof HTMLDialogElement.prototype.close !== 'function') {
  HTMLDialogElement.prototype.close = function (this: HTMLDialogElement) {
    this.removeAttribute('open');
    this.dispatchEvent(new Event('close'));
  };
}

describe('AccountDeskTransactionHistoryComponent', () => {
  it('loads only a selected receipt into an accessible drawer and restores focus', async () => {
    const detail = vi.fn().mockResolvedValue({
      transaction_id: 'ctxn_1', account_id: 'DU1234567', journal_seq: 4, recorded_at_ms: 1_780_000_000_000,
      transaction_kind: 'manual_ibkr_acknowledgement', strategy_instance_id: 'manual', run_id: 'manual', intent_id: 'intent/opaque', order_ref: 'manual/v1:opaque', order_id: 42, perm_id: null, exec_id: 'exec/opaque', lifecycle_state: 'filled', commission_status: 'reported', fee: 1.25,
      receipt: {
        order_ref: 'manual/v1:opaque',
        receipt_hash: 'sha256:opaque',
        broker_event: {
          reason_code: 'ACCOUNT_CLERK_UNAVAILABLE',
          lifecycle_state: 'partially_filled',
          evidence_ref: 'nested/opaque',
        },
      },
      custody_timeline: {
        intent_created_at_ms: 1_780_000_000_000,
        clerk_request_received_at_ms: 1_780_000_000_000,
        clerk_intake_admitted_at_ms: 1_780_000_000_000,
        inbox_fsynced_at_ms: 1_780_000_000_000,
        a0_custody_accepted_at_ms: 1_780_000_000_000,
        broker_write_started_at_ms: 1_780_000_000_001,
        broker_call_returned_at_ms: null,
        broker_ack_recorded_at_ms: null,
        earliest_broker_source_at_ms: null,
        first_callback_arrived_at_ms: null,
        first_callback_recorded_at_ms: null,
        economic_terminal_recorded_at_ms: 1_780_000_000_002,
        durations: {
          request_to_intake_ms: 0,
          intake_to_a0_ms: 0,
          a0_to_broker_write_ms: 1,
          broker_write_to_return_ms: null,
          broker_return_to_first_callback_ms: null,
          terminal_age_ms: 2,
        },
      },
      events: [{ event_id: 'event_1', event_kind: 'execution', callback_identity: 'callback/opaque', lifecycle_state: 'filled', commission_status: 'reported', fee: 1.25, journal_seq: 5, recorded_at_ms: 1_780_000_000_001, receipt: { execution_ref: 'exec/opaque' }}],
    });
    const store = {
      accountId: signal('DU1234567'),
      loading: signal(false), errorMessage: signal<string | null>(null), hasLastGood: signal(true),
      feed: signal({ projection_available: true, canonical_fallback_required: false, feed_state: 'live', feed_headline: 'Live', feed_detail: 'Current', high_water_journal_seq: 5, lag_records: 0, lag_is_lower_bound: false, rows: [], next_cursor: null }),
      rows: signal([{ transaction_id: 'ctxn_1', account_id: 'DU1234567', journal_seq: 4, recorded_at_ms: 1_780_000_000_000, transaction_kind: 'manual_ibkr_acknowledgement', strategy_instance_id: 'manual', run_id: 'manual', intent_id: 'intent/opaque', order_ref: 'manual/v1:opaque', order_id: 42, perm_id: null, exec_id: 'exec/opaque', lifecycle_state: 'filled', commission_status: 'reported' as const, fee: 1.25, event_count: 1 }]),
      nextCursor: signal<string | null>(null), retry: vi.fn(), loadOlder: vi.fn(), transactionDetail: detail,
    };
    await render(AccountDeskTransactionHistoryComponent, {
      providers: [
        { provide: AccountDeskTransactionHistoryStore, useValue: store },
        { provide: BrokerService, useValue: { accountTransaction: detail } },
      ],
    });

    const row = screen.getByRole('button', { name: /open receipt manual\/v1:opaque/i });
    fireEvent.click(row);
    expect(detail).toHaveBeenCalledWith('DU1234567', 'ctxn_1');
    expect(await screen.findByText('Projected transaction evidence')).toBeTruthy();
    expect(screen.getAllByText('manual/v1:opaque').length).toBeGreaterThan(0);
    expect(screen.getAllByText('sha256:opaque').length).toBeGreaterThan(0);
    expect(screen.getByText('Account Clerk Unavailable')).toBeTruthy();
    expect(screen.getByText('Partially Filled')).toBeTruthy();
    expect(screen.getByText('nested/opaque')).toBeTruthy();
    expect(screen.getByText('Custody clocks')).toBeTruthy();
    expect(screen.getByText(/Record order is durable serialization order/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Close receipt drawer', hidden: true }));
    expect(document.activeElement).toBe(row);
  });

  it('has a compact mobile-readable label for every transaction cell', async () => {
    const store = { accountId: signal('DU1234567'), loading: signal(false), errorMessage: signal<string | null>(null), hasLastGood: signal(true), feed: signal({ projection_available: true, canonical_fallback_required: false, feed_state: 'live', feed_headline: 'Live', feed_detail: 'Current', high_water_journal_seq: 1, lag_records: 0, lag_is_lower_bound: false, rows: [], next_cursor: null }), rows: signal([]), nextCursor: signal<string | null>(null), retry: vi.fn(), loadOlder: vi.fn(), transactionDetail: vi.fn() };
    await render(AccountDeskTransactionHistoryComponent, {
      providers: [
        { provide: AccountDeskTransactionHistoryStore, useValue: store },
        { provide: BrokerService, useValue: { accountTransaction: vi.fn() } },
      ],
    });
    expect(screen.getByText(/No projected transactions match/)).toBeTruthy();
  });

  it('renders SQLite origin labels and execution facts without client-side classification', async () => {
    const store = {
      accountId: signal('PA1'),
      loading: signal(false),
      errorMessage: signal<string | null>(null),
      hasLastGood: signal(true),
      feed: signal({ projection_available: true, canonical_fallback_required: false, feed_state: 'live', feed_headline: 'SQLite projection current', feed_detail: 'Current', high_water_journal_seq: 3, lag_records: 0, lag_is_lower_bound: false, rows: [], next_cursor: null }),
      rows: signal([
        sqliteRow({ transaction_id: 'strategy-row', transaction_origin: 'strategy', strategy_instance_id: 'bot-googl', run_id: 'run-googl', execution_quantity: 3, execution_price: 175.25, fee_fidelity: 'reported', fee: 0.1 }),
        sqliteRow({ transaction_id: 'external-row', transaction_origin: 'external', strategy_instance_id: null, run_id: null, intent_id: null, order_ref: null, external_order_id: 'alpaca-external-1', execution_quantity: 1, execution_price: 99.5, fee_fidelity: 'not_reported' }),
        sqliteRow({ transaction_id: 'unknown-row', transaction_origin: 'unknown', strategy_instance_id: null, run_id: null, intent_id: null, order_ref: null }),
        sqliteRow({ transaction_id: 'emergency-row', transaction_origin: 'emergency', strategy_instance_id: null, run_id: null, intent_id: null, order_ref: 'emergency/v1:opaque' }),
      ]),
      nextCursor: signal<string | null>(null), retry: vi.fn(), loadOlder: vi.fn(), transactionDetail: vi.fn(),
    };
    await render(AccountDeskTransactionHistoryComponent, {
      providers: [
        { provide: AccountDeskTransactionHistoryStore, useValue: store },
        { provide: BrokerService, useValue: { accountTransaction: vi.fn() } },
      ],
    });

    expect(screen.getByText('Placed by bot-googl')).toBeTruthy();
    expect(screen.getByText('External / manual')).toBeTruthy();
    expect(screen.getByText('Unknown — review required')).toBeTruthy();
    expect(screen.getByText('alpaca-external-1')).toBeTruthy();
    // A system-initiated safety-flatten origin must never collapse into the
    // "External / manual" label — that would misrepresent it as an
    // unattributed order rather than a Clerk-owned safety action. Scope to
    // the row itself: "Emergency" also appears as an origin-filter option.
    expect(
      screen.getByRole('button', { name: /open receipt emergency\/v1:opaque/i }).textContent,
    ).toContain('Emergency');
    expect(screen.getByText(/Execution: 3/)).toBeTruthy();
    expect(
      screen.getByRole('button', { name: /open receipt learn-ai\/bot-default\/v1:intent-default/i })
        .textContent,
    ).toMatch(/reported/i);
    expect(screen.getByText('Fees not reported')).toBeTruthy();
  });

  it('renders unknown custody clocks and epoch provenance for a recorded-only legacy receipt', async () => {
    const detail = vi.fn().mockResolvedValue({
      transaction_id: 'ctxn_legacy', account_id: 'DU1234567', journal_seq: 4, recorded_at_ms: 1_780_000_000_000,
      transaction_kind: 'strategy_ibkr_order', strategy_instance_id: 'bot-a', run_id: 'run-a', intent_id: 'intent/legacy', order_ref: 'order/legacy', order_id: null, perm_id: null, exec_id: null, lifecycle_state: 'recorded', commission_status: 'unknown', fee: null, receipt: { legacy: true }, events: [], custody_timeline: null,
    });
    const store = {
      accountId: signal('DU1234567'), loading: signal(false), errorMessage: signal<string | null>(null), hasLastGood: signal(true),
      feed: signal({ projection_available: true, canonical_fallback_required: false, feed_state: 'live', feed_headline: 'Live', feed_detail: 'Current', high_water_journal_seq: 4, lag_records: 0, lag_is_lower_bound: false, rows: [], next_cursor: null }),
      rows: signal([{ transaction_id: 'ctxn_legacy', account_id: 'DU1234567', journal_seq: 4, recorded_at_ms: 1_780_000_000_000, transaction_kind: 'strategy_ibkr_order', strategy_instance_id: 'bot-a', run_id: 'run-a', intent_id: 'intent/legacy', order_ref: 'order/legacy', order_id: null, perm_id: null, exec_id: null, lifecycle_state: 'recorded' as const, commission_status: 'unknown' as const, fee: null, event_count: 1 }]),
      nextCursor: signal<string | null>(null), retry: vi.fn(), loadOlder: vi.fn(),
    };
    await render(AccountDeskTransactionHistoryComponent, {
      providers: [
        { provide: AccountDeskTransactionHistoryStore, useValue: store },
        { provide: BrokerService, useValue: { accountTransaction: detail } },
      ],
    });

    fireEvent.click(screen.getByRole('button', { name: /open receipt order\/legacy/i }));
    expect(await screen.findByText('Custody clocks')).toBeTruthy();
    expect(screen.getByText('Origin epoch provenance')).toBeTruthy();
    expect(screen.getByText('Observed epoch provenance')).toBeTruthy();
    expect(screen.getAllByText('Unknown').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Unknown (not recorded)').length).toBe(2);
  });

  it('keeps the latest selected receipt when a prior detail request resolves late', async () => {
    let firstResolve: ((value: object) => void) | undefined;
    const first = new Promise<object>((resolve) => { firstResolve = resolve; });
    const detail = vi.fn()
      .mockReturnValueOnce(first)
      .mockResolvedValueOnce({
        transaction_id: 'ctxn_2', account_id: 'DU1234567', journal_seq: 5, recorded_at_ms: 1_780_000_000_001,
        transaction_kind: 'manual_ibkr_acknowledgement', strategy_instance_id: 'manual', run_id: 'manual', intent_id: 'intent/new', order_ref: 'manual/v1:new', order_id: 43, perm_id: null, exec_id: null, lifecycle_state: 'filled', commission_status: 'unknown', fee: null, receipt: { receipt_hash: 'latest' }, events: [],
      });
    const store = {
      accountId: signal('DU1234567'),
      loading: signal(false), errorMessage: signal<string | null>(null), hasLastGood: signal(true),
      feed: signal({ projection_available: true, canonical_fallback_required: false, feed_state: 'live', feed_headline: 'Live', feed_detail: 'Current', high_water_journal_seq: 5, lag_records: 0, rows: [], next_cursor: null }),
      rows: signal([
        { transaction_id: 'ctxn_1', account_id: 'DU1234567', journal_seq: 4, recorded_at_ms: 1_780_000_000_000, transaction_kind: 'manual_ibkr_acknowledgement', strategy_instance_id: 'manual', run_id: 'manual', intent_id: 'intent/old', order_ref: 'manual/v1:old', order_id: 42, perm_id: null, exec_id: null, lifecycle_state: 'submitted', commission_status: 'unknown' as const, fee: null, event_count: 1 },
        { transaction_id: 'ctxn_2', account_id: 'DU1234567', journal_seq: 5, recorded_at_ms: 1_780_000_000_001, transaction_kind: 'manual_ibkr_acknowledgement', strategy_instance_id: 'manual', run_id: 'manual', intent_id: 'intent/new', order_ref: 'manual/v1:new', order_id: 43, perm_id: null, exec_id: null, lifecycle_state: 'filled', commission_status: 'unknown' as const, fee: null, event_count: 1 },
      ]),
      nextCursor: signal<string | null>(null), retry: vi.fn(), loadOlder: vi.fn(), transactionDetail: detail,
    };
    await render(AccountDeskTransactionHistoryComponent, {
      providers: [
        { provide: AccountDeskTransactionHistoryStore, useValue: store },
        { provide: BrokerService, useValue: { accountTransaction: detail } },
      ],
    });

    fireEvent.click(screen.getByRole('button', { name: /open receipt manual\/v1:old/i }));
    fireEvent.click(screen.getByRole('button', { name: /open receipt manual\/v1:new/i }));
    expect(await screen.findByText('latest')).toBeTruthy();

    firstResolve?.({
      transaction_id: 'ctxn_1', account_id: 'DU1234567', journal_seq: 4, recorded_at_ms: 1_780_000_000_000,
      transaction_kind: 'manual_ibkr_acknowledgement', strategy_instance_id: 'manual', run_id: 'manual', intent_id: 'intent/old', order_ref: 'manual/v1:old', order_id: 42, perm_id: null, exec_id: null, lifecycle_state: 'submitted', commission_status: 'unknown', fee: null, receipt: { receipt_hash: 'stale' }, events: [],
    });
    await Promise.resolve();

    expect(screen.getByText('latest')).toBeTruthy();
    expect(screen.queryByText('stale')).toBeNull();
  });

  it('invalidates an open receipt request when the selected account changes', async () => {
    let resolveDetail: ((value: object) => void) | undefined;
    const detail = new Promise<object>((resolve) => { resolveDetail = resolve; });
    const accountId = signal('DU1234567');
    const store = {
      accountId,
      loading: signal(false), errorMessage: signal<string | null>(null), hasLastGood: signal(true),
      feed: signal({ projection_available: true, canonical_fallback_required: false, feed_state: 'live', feed_headline: 'Live', feed_detail: 'Current', high_water_journal_seq: 4, lag_records: 0, lag_is_lower_bound: false, rows: [], next_cursor: null }),
      rows: signal([{ transaction_id: 'ctxn_1', account_id: 'DU1234567', journal_seq: 4, recorded_at_ms: 1_780_000_000_000, transaction_kind: 'manual_ibkr_acknowledgement', strategy_instance_id: 'manual', run_id: 'manual', intent_id: 'intent/old', order_ref: 'manual/v1:old', order_id: 42, perm_id: null, exec_id: null, lifecycle_state: 'submitted' as const, commission_status: 'unknown' as const, fee: null, event_count: 1 }]),
      nextCursor: signal<string | null>(null), retry: vi.fn(), loadOlder: vi.fn(), transactionDetail: vi.fn().mockReturnValue(detail),
    };
    await render(AccountDeskTransactionHistoryComponent, {
      providers: [
        { provide: AccountDeskTransactionHistoryStore, useValue: store },
        { provide: BrokerService, useValue: { accountTransaction: store.transactionDetail } },
      ],
    });

    fireEvent.click(screen.getByRole('button', { name: /open receipt manual\/v1:old/i }));
    accountId.set('DU7654321');
    await Promise.resolve();
    resolveDetail?.({
      transaction_id: 'ctxn_1', account_id: 'DU1234567', journal_seq: 4, recorded_at_ms: 1_780_000_000_000,
      transaction_kind: 'manual_ibkr_acknowledgement', strategy_instance_id: 'manual', run_id: 'manual', intent_id: 'intent/old', order_ref: 'manual/v1:old', order_id: 42, perm_id: null, exec_id: null, lifecycle_state: 'submitted', commission_status: 'unknown', fee: null, receipt: { receipt_hash: 'stale-account-detail' }, events: [],
    });
    await Promise.resolve();

    expect(screen.queryByText('stale-account-detail')).toBeNull();
  });
});

function sqliteRow(overrides: Record<string, unknown>) {
  return {
    transaction_id: 'sqlite-row', broker: 'alpaca', account_id: 'PA1', journal_seq: 1,
    recorded_at_ms: 1_780_000_000_000, transaction_kind: 'sqlite_execution',
    transaction_origin: 'strategy', strategy_instance_id: 'bot-default', run_id: 'run-default',
    intent_id: 'intent-default', order_ref: 'learn-ai/bot-default/v1:intent-default',
    order_id: null, perm_id: null, exec_id: null, native_order_id: 'order-default',
    native_execution_id: 'execution-default', lifecycle_state: 'filled',
    commission_status: 'unknown', fee: null, execution_quantity: null, execution_price: null,
    fee_fidelity: null, event_count: 1,
    order_instruction: {
      symbol: 'GOOGL', sec_type: 'us_equity', action: 'buy', quantity: 1,
      order_type: 'market', limit_price: null, time_in_force: 'day', outside_rth: false,
    },
    ...overrides,
  };
}
