import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type {
  ClerkTransactionHistoryResponse,
  ClerkTransactionSummary,
} from '../../../api/clerk-transaction-history.types';
import { BrokersService } from '../../../services/brokers.service';
import { AccountDeskTransactionHistoryStore } from './account-desk-transaction-history-store.service';

describe('AccountDeskTransactionHistoryStore', () => {
  const broker = { accountTransactions: vi.fn() };

  beforeEach(() => {
    broker.accountTransactions.mockReset();
    TestBed.configureTestingModule({
      providers: [
        provideZonelessChangeDetection(),
        AccountDeskTransactionHistoryStore,
        { provide: BrokersService, useValue: broker },
      ],
    });
  });

  afterEach(() => TestBed.resetTestingModule());

  it('loads every keyset page in the selected server-bounded period before publishing rows', async () => {
    let resolveSecond: ((page: ClerkTransactionHistoryResponse) => void) | undefined;
    broker.accountTransactions
      .mockResolvedValueOnce(historyPage([transaction('older', 10)], 'cursor-2'))
      .mockReturnValueOnce(new Promise<ClerkTransactionHistoryResponse>((resolve) => {
        resolveSecond = resolve;
      }));
    const store = TestBed.inject(AccountDeskTransactionHistoryStore);

    const loading = store.load('PA1', { fromMs: 1_700_000_000_000, toMs: 1_700_086_400_000 });
    await vi.waitFor(() => expect(broker.accountTransactions).toHaveBeenCalledTimes(2));

    expect(store.rows()).toEqual([]);
    expect(store.loadedCount()).toBe(1);
    expect(broker.accountTransactions).toHaveBeenNthCalledWith(1, 'PA1', null, 100, {
      fromMs: 1_700_000_000_000,
      toMs: 1_700_086_400_000,
    });
    expect(broker.accountTransactions).toHaveBeenNthCalledWith(2, 'PA1', 'cursor-2', 100, {
      fromMs: 1_700_000_000_000,
      toMs: 1_700_086_400_000,
    });

    resolveSecond?.(historyPage([transaction('newer', 20)], null));
    await loading;

    expect(store.rows().map((row) => row.transaction_id)).toEqual(['newer', 'older']);
    expect(store.loadedPages()).toBe(2);
  });

  it('discards a late period response after the selected window changes', async () => {
    let resolveInitial: ((page: ClerkTransactionHistoryResponse) => void) | undefined;
    broker.accountTransactions
      .mockReturnValueOnce(new Promise<ClerkTransactionHistoryResponse>((resolve) => {
        resolveInitial = resolve;
      }))
      .mockResolvedValueOnce(historyPage([transaction('current', 20)]));
    const store = TestBed.inject(AccountDeskTransactionHistoryStore);

    void store.load('PA1', { fromMs: 1, toMs: 10 });
    await store.load('PA1', { fromMs: 11, toMs: 20 });
    resolveInitial?.(historyPage([transaction('stale', 30)]));
    await Promise.resolve();

    expect(store.rows().map((row) => row.transaction_id)).toEqual(['current']);
    expect(store.filters()).toEqual({ fromMs: 11, toMs: 20 });
  });

  it('queues a refresh that arrives while the same period is loading', async () => {
    let resolveInitial: ((page: ClerkTransactionHistoryResponse) => void) | undefined;
    broker.accountTransactions
      .mockReturnValueOnce(new Promise<ClerkTransactionHistoryResponse>((resolve) => {
        resolveInitial = resolve;
      }))
      .mockResolvedValueOnce(historyPage());
    const store = TestBed.inject(AccountDeskTransactionHistoryStore);

    void store.load('PA1');
    await store.load('PA1');
    expect(broker.accountTransactions).toHaveBeenCalledTimes(1);

    resolveInitial?.(historyPage());
    await vi.waitFor(() => expect(broker.accountTransactions).toHaveBeenCalledTimes(2));
    expect(broker.accountTransactions).toHaveBeenLastCalledWith('PA1', null, 100, {});
  });

  it('stops at the explicit client safety limit even when more cursors remain', async () => {
    broker.accountTransactions.mockImplementation((_: string, cursor: string | null) => {
      const pageNumber = cursor === null ? 0 : Number(cursor);
      const rows = Array.from({ length: 100 }, (_, index) => {
        const sequence = pageNumber * 100 + index;
        return transaction(`transaction-${sequence}`, sequence);
      });
      return Promise.resolve(historyPage(rows, String(pageNumber + 1)));
    });
    const store = TestBed.inject(AccountDeskTransactionHistoryStore);

    await store.load('PA1');

    expect(broker.accountTransactions).toHaveBeenCalledTimes(50);
    expect(store.rows()).toHaveLength(5_000);
    expect(store.rowLimitReached()).toBe(true);
  });
});

function transaction(transactionId: string, recordedAtMs: number): ClerkTransactionSummary {
  return {
    transaction_id: transactionId,
    broker: 'alpaca',
    account_id: 'PA1',
    journal_seq: recordedAtMs,
    recorded_at_ms: recordedAtMs,
    transaction_kind: 'sqlite_order',
    strategy_instance_id: null,
    run_id: null,
    intent_id: null,
    order_ref: null,
    order_id: null,
    perm_id: null,
    exec_id: null,
    native_order_id: null,
    native_execution_id: null,
    lifecycle_state: 'submitted',
    commission_status: 'unknown',
    fee: null,
    event_count: 1,
  };
}

function historyPage(
  rows: ClerkTransactionSummary[] = [],
  nextCursor: string | null = null,
): ClerkTransactionHistoryResponse {
  return {
    projection_available: true,
    canonical_fallback_required: false,
    feed_state: 'live',
    feed_headline: 'Live',
    feed_detail: 'Current',
    high_water_journal_seq: 1,
    lag_records: 0,
    lag_is_lower_bound: false,
    custody_summary: {
      record_count: rows.length,
      a0_custody_accepted_count: 0,
      a1_broker_write_started_count: 0,
      a2_broker_known_count: 0,
      a3_economic_terminal_count: 0,
      uncertain_count: 0,
    },
    rows,
    next_cursor: nextCursor,
  };
}
