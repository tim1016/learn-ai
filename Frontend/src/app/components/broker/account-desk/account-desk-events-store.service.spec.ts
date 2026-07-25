import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { AccountEventsResponse, TraderAccountEventsResponse } from '../../../api/account-events.types';
import { BrokerService } from '../../../services/broker.service';
import { AccountDeskEventsStore } from './account-desk-events-store.service';

describe('AccountDeskEventsStore', () => {
  const broker = { accountEvents: vi.fn(), traderAccountEvents: vi.fn() };

  beforeEach(() => {
    broker.accountEvents.mockReset();
    broker.traderAccountEvents.mockReset();
    TestBed.configureTestingModule({
      providers: [provideZonelessChangeDetection(), AccountDeskEventsStore, { provide: BrokerService, useValue: broker }],
    });
  });

  afterEach(() => TestBed.resetTestingModule());

  it('requests the operator schema only after the operator lens is opened', async () => {
    broker.accountEvents.mockResolvedValue(operationsPage());
    broker.traderAccountEvents.mockResolvedValue(traderPage());
    const store = TestBed.inject(AccountDeskEventsStore);

    await store.load('DU1234567');
    store.loadOperations();
    await Promise.resolve();

    expect(store.traderRows()[0]).toEqual(expect.objectContaining({ outcome: 'Backend outcome 2.' }));
    expect(store.traderRows()[0]).not.toHaveProperty('operator_order_receipt');
    expect(store.operationRows()[0]).toHaveProperty('operator_detail');
    expect(broker.traderAccountEvents).toHaveBeenCalledWith('DU1234567', 100);
    expect(broker.accountEvents).toHaveBeenCalledWith('DU1234567', expect.objectContaining({ view: 'operations' }));
  });

  it('retains a last-good trader page when the trader endpoint later fails', async () => {
    broker.accountEvents.mockResolvedValue(operationsPage());
    broker.traderAccountEvents
      .mockResolvedValueOnce(traderPage())
      .mockRejectedValueOnce(new Error('offline'));
    const store = TestBed.inject(AccountDeskEventsStore);

    await store.load('DU1234567');
    await store.load('DU1234567');

    expect(store.traderRows()).toHaveLength(1);
    expect(store.traderShowingStaleLastGood()).toBe(true);
  });
});

function traderPage(): TraderAccountEventsResponse {
  return {
    schema_version: 1,
    account_id: 'DU1234567',
    rows: [{ schema_version: 1, event_id: 'DU1234567:2', seq: 2, occurred_at_ms: 1_780_000_000_002, outcome: 'Backend outcome 2.' }],
    latest_seq: 2,
    next_before_seq: null,
  };
}

function operationsPage(): AccountEventsResponse {
  return {
    schema_version: 1,
    account_id: 'DU1234567',
    view: 'operations',
    rows: [{ schema_version: 1, event_id: 'DU1234567:2', seq: 2, kind: 'safety', occurred_at_ms: 1_780_000_000_002, trader_narration: 'Backend outcome 2.', operator_detail: 'Operator detail.', evidence_refs: [], operator_order_receipt: null }],
    latest_seq: 2,
    next_before_seq: null,
  };
}
