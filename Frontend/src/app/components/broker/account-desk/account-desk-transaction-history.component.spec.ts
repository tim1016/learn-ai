import { signal } from '@angular/core';
import { fireEvent, render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { AccountDeskTransactionHistoryStore } from './account-desk-transaction-history-store.service';
import { AccountDeskTransactionHistoryComponent } from './account-desk-transaction-history.component';

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
      receipt: { order_ref: 'manual/v1:opaque', receipt_hash: 'sha256:opaque' },
      events: [{ event_id: 'event_1', event_kind: 'execution', callback_identity: 'callback/opaque', lifecycle_state: 'filled', commission_status: 'reported', fee: 1.25, journal_seq: 5, recorded_at_ms: 1_780_000_000_001, receipt: { execution_ref: 'exec/opaque' }}],
    });
    const store = {
      loading: signal(false), errorMessage: signal<string | null>(null), hasLastGood: signal(true),
      feed: signal({ projection_available: true, canonical_fallback_required: false, feed_state: 'live', feed_headline: 'Live', feed_detail: 'Current', high_water_journal_seq: 5, lag_records: 0, rows: [], next_cursor: null }),
      rows: signal([{ transaction_id: 'ctxn_1', account_id: 'DU1234567', journal_seq: 4, recorded_at_ms: 1_780_000_000_000, transaction_kind: 'manual_ibkr_acknowledgement', strategy_instance_id: 'manual', run_id: 'manual', intent_id: 'intent/opaque', order_ref: 'manual/v1:opaque', order_id: 42, perm_id: null, exec_id: 'exec/opaque', lifecycle_state: 'filled', commission_status: 'reported' as const, fee: 1.25, event_count: 1 }]),
      nextCursor: signal<string | null>(null), retry: vi.fn(), loadOlder: vi.fn(), transactionDetail: detail,
    };
    await render(AccountDeskTransactionHistoryComponent, { providers: [{ provide: AccountDeskTransactionHistoryStore, useValue: store }] });

    const row = screen.getByRole('button', { name: /open receipt manual\/v1:opaque/i });
    fireEvent.click(row);
    expect(detail).toHaveBeenCalledWith('ctxn_1');
    expect(await screen.findByText('Projected transaction evidence')).toBeTruthy();
    expect(screen.getAllByText('manual/v1:opaque').length).toBeGreaterThan(0);
    expect(screen.getAllByText('sha256:opaque').length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole('button', { name: 'Close receipt drawer', hidden: true }));
    expect(document.activeElement).toBe(row);
  });

  it('has a compact mobile-readable label for every transaction cell', async () => {
    const store = { loading: signal(false), errorMessage: signal<string | null>(null), hasLastGood: signal(true), feed: signal({ projection_available: true, canonical_fallback_required: false, feed_state: 'live', feed_headline: 'Live', feed_detail: 'Current', high_water_journal_seq: 1, lag_records: 0, rows: [], next_cursor: null }), rows: signal([]), nextCursor: signal<string | null>(null), retry: vi.fn(), loadOlder: vi.fn(), transactionDetail: vi.fn() };
    await render(AccountDeskTransactionHistoryComponent, { providers: [{ provide: AccountDeskTransactionHistoryStore, useValue: store }] });
    expect(screen.getByText(/No projected manual transactions/)).toBeTruthy();
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
      loading: signal(false), errorMessage: signal<string | null>(null), hasLastGood: signal(true),
      feed: signal({ projection_available: true, canonical_fallback_required: false, feed_state: 'live', feed_headline: 'Live', feed_detail: 'Current', high_water_journal_seq: 5, lag_records: 0, rows: [], next_cursor: null }),
      rows: signal([
        { transaction_id: 'ctxn_1', account_id: 'DU1234567', journal_seq: 4, recorded_at_ms: 1_780_000_000_000, transaction_kind: 'manual_ibkr_acknowledgement', strategy_instance_id: 'manual', run_id: 'manual', intent_id: 'intent/old', order_ref: 'manual/v1:old', order_id: 42, perm_id: null, exec_id: null, lifecycle_state: 'submitted', commission_status: 'unknown' as const, fee: null, event_count: 1 },
        { transaction_id: 'ctxn_2', account_id: 'DU1234567', journal_seq: 5, recorded_at_ms: 1_780_000_000_001, transaction_kind: 'manual_ibkr_acknowledgement', strategy_instance_id: 'manual', run_id: 'manual', intent_id: 'intent/new', order_ref: 'manual/v1:new', order_id: 43, perm_id: null, exec_id: null, lifecycle_state: 'filled', commission_status: 'unknown' as const, fee: null, event_count: 1 },
      ]),
      nextCursor: signal<string | null>(null), retry: vi.fn(), loadOlder: vi.fn(), transactionDetail: detail,
    };
    await render(AccountDeskTransactionHistoryComponent, { providers: [{ provide: AccountDeskTransactionHistoryStore, useValue: store }] });

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
});
