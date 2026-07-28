import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { BrokerService } from '../../../services/broker.service';
import { AccountDeskTransactionHistoryStore } from './account-desk-transaction-history-store.service';

describe('AccountDeskTransactionHistoryStore', () => {
  const broker = { accountTransactions: vi.fn() };

  beforeEach(() => {
    broker.accountTransactions.mockReset();
    TestBed.configureTestingModule({
      providers: [
        provideZonelessChangeDetection(),
        AccountDeskTransactionHistoryStore,
        { provide: BrokerService, useValue: broker },
      ],
    });
  });

  afterEach(() => TestBed.resetTestingModule());

  it('requests a 25-row summary page', async () => {
    broker.accountTransactions.mockResolvedValue(historyPage());
    const store = TestBed.inject(AccountDeskTransactionHistoryStore);

    await store.load('DU1234567');

    expect(broker.accountTransactions).toHaveBeenCalledTimes(1);
    expect(broker.accountTransactions).toHaveBeenCalledWith('DU1234567', null, 25, {});
  });

  it('replaces an in-flight page with a server-filtered bounded request', async () => {
    let resolveInitial: ((value: ReturnType<typeof historyPage>) => void) | undefined;
    broker.accountTransactions
      .mockReturnValueOnce(new Promise<ReturnType<typeof historyPage>>((resolve) => { resolveInitial = resolve; }))
      .mockResolvedValueOnce(historyPage());
    const store = TestBed.inject(AccountDeskTransactionHistoryStore);

    void store.load('DU1234567');
    store.setFilters({ origin: 'strategy', lifecycleState: 'submitted' });

    expect(broker.accountTransactions).toHaveBeenLastCalledWith('DU1234567', null, 25, {
      origin: 'strategy', lifecycleState: 'submitted', strategyInstanceId: null, runId: null,
    });
    resolveInitial?.(historyPage());
    await Promise.resolve();
    expect(store.loading()).toBe(false);
  });
});

function historyPage() {
  return {
    projection_available: true,
    canonical_fallback_required: false,
    feed_state: 'live' as const,
    feed_headline: 'Live',
    feed_detail: 'Current',
    high_water_journal_seq: 1,
    lag_records: 0,
    lag_is_lower_bound: false,
    custody_summary: {
      record_count: 0,
      a0_custody_accepted_count: 0,
      a1_broker_write_started_count: 0,
      a2_broker_known_count: 0,
      a3_economic_terminal_count: 0,
      uncertain_count: 0,
    },
    rows: [],
    next_cursor: null,
  };
}
