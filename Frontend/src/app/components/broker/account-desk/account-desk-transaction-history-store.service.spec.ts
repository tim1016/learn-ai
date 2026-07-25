import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { BrokerService } from '../../../services/broker.service';
import { AccountDeskTransactionHistoryStore } from './account-desk-transaction-history-store.service';

describe('AccountDeskTransactionHistoryStore', () => {
  const broker = { accountTransactions: vi.fn(), accountTransaction: vi.fn() };

  beforeEach(() => {
    broker.accountTransactions.mockReset();
    broker.accountTransaction.mockReset();
    TestBed.configureTestingModule({
      providers: [
        provideZonelessChangeDetection(),
        AccountDeskTransactionHistoryStore,
        { provide: BrokerService, useValue: broker },
      ],
    });
  });

  afterEach(() => TestBed.resetTestingModule());

  it('requests a 25-row summary page and reads only a selected detail receipt', async () => {
    broker.accountTransactions.mockResolvedValue(historyPage());
    broker.accountTransaction.mockResolvedValue({ transaction_id: 'ctxn_1', receipt: {}, events: [] });
    const store = TestBed.inject(AccountDeskTransactionHistoryStore);

    await store.load('DU1234567');
    await store.transactionDetail('ctxn_1');

    expect(broker.accountTransactions).toHaveBeenCalledTimes(1);
    expect(broker.accountTransactions).toHaveBeenCalledWith('DU1234567', null, 25);
    expect(broker.accountTransaction).toHaveBeenCalledWith('DU1234567', 'ctxn_1');
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
    rows: [],
    next_cursor: null,
  };
}
