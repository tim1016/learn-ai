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

  it('merges a delayed earlier callback by stable event identity instead of positional sequence', async () => {
    broker.traderAccountEvents.mockResolvedValue(traderPage());
    broker.accountEvents
      .mockResolvedValueOnce(operationsPage())
      .mockResolvedValueOnce(delayedEarlierOperationsPage());
    const store = TestBed.inject(AccountDeskEventsStore);

    await store.load('DU1234567');
    store.loadOperations();
    await vi.waitFor(() => expect(store.operationRows()).toHaveLength(1));

    (store as unknown as { pollForNewer(): void }).pollForNewer();
    await vi.waitFor(() => expect(store.operationRows()).toHaveLength(3));

    expect(store.operationRows().map((row) => row.event_id)).toEqual([
      'DU1234567:2',
      'DU1234567:1',
      'producer:DU1234567:data_plane:late-callback',
    ]);
    expect(broker.accountEvents).toHaveBeenLastCalledWith('DU1234567', expect.objectContaining({
      view: 'operations',
    }));
    const [, lastRequest] = broker.accountEvents.mock.calls.at(-1) as [string, { afterSeq?: number }];
    expect(lastRequest).not.toHaveProperty('afterSeq');
  });

  it('keeps the middle live window reachable after more than one head page arrives', async () => {
    broker.traderAccountEvents.mockResolvedValue(traderPage());
    broker.accountEvents
      .mockResolvedValueOnce(operationsWindow(100, 51, 51))
      .mockResolvedValueOnce(operationsWindow(200, 151, 151))
      .mockResolvedValueOnce(operationsWindow(150, 101, 101));
    const store = TestBed.inject(AccountDeskEventsStore);

    await store.load('DU1234567');
    store.loadOperations();
    await vi.waitFor(() => expect(store.operationRows()).toHaveLength(50));

    (store as unknown as { pollForNewer(): void }).pollForNewer();
    await vi.waitFor(() => expect(store.operationRows()).toHaveLength(100));
    expect(store.nextBeforeSeq()).toBe(151);

    store.loadOlder();
    await vi.waitFor(() => expect(store.operationRows()).toHaveLength(150));

    expect(store.operationRows().map((row) => row.seq)).toEqual(
      Array.from({ length: 150 }, (_, index) => 200 - index),
    );
    expect(broker.accountEvents).toHaveBeenLastCalledWith('DU1234567', expect.objectContaining({
      beforeSeq: 151,
      view: 'operations',
    }));
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
    rows: [{ schema_version: 1, event_id: 'DU1234567:2', provenance: 'producer_operational_log', seq: 2, kind: 'safety', occurred_at_ms: 1_780_000_000_002, event_at_ms: null, arrived_at_ms: null, recorded_at_ms: 1_780_000_000_002, producer: 'data_plane', producer_boot_id: 'boot-a', producer_seq: 2, trader_narration: 'Backend outcome 2.', operator_detail: 'Operator detail.', evidence_refs: [], operator_order_receipt: null }],
    latest_seq: 2,
    next_before_seq: null,
  };
}

function delayedEarlierOperationsPage(): AccountEventsResponse {
  return {
    schema_version: 1,
    account_id: 'DU1234567',
    view: 'operations',
    rows: [
      {
        schema_version: 1,
        event_id: 'DU1234567:2',
        provenance: 'producer_operational_log',
        seq: 3,
        kind: 'safety',
        occurred_at_ms: 1_780_000_000_002,
        event_at_ms: null,
        arrived_at_ms: null,
        recorded_at_ms: 1_780_000_000_002,
        producer: 'data_plane',
        producer_boot_id: 'boot-a',
        producer_seq: 2,
        trader_narration: 'Backend outcome 2.',
        operator_detail: 'Operator detail.',
        evidence_refs: [],
        operator_order_receipt: null,
      },
      {
        schema_version: 1,
        event_id: 'DU1234567:1',
        provenance: 'producer_operational_log',
        seq: 2,
        kind: 'safety',
        occurred_at_ms: 1_780_000_000_001,
        event_at_ms: null,
        arrived_at_ms: null,
        recorded_at_ms: 1_780_000_000_001,
        producer: 'data_plane',
        producer_boot_id: 'boot-a',
        producer_seq: 1,
        trader_narration: null,
        operator_detail: 'Operator detail 1.',
        evidence_refs: [],
        operator_order_receipt: null,
      },
      {
        schema_version: 1,
        event_id: 'producer:DU1234567:data_plane:late-callback',
        provenance: 'producer_operational_log',
        seq: 1,
        kind: 'safety',
        occurred_at_ms: 1,
        event_at_ms: 1,
        arrived_at_ms: 1_780_000_000_003,
        recorded_at_ms: 1_780_000_000_003,
        producer: 'data_plane',
        producer_boot_id: 'boot-b',
        producer_seq: 1,
        trader_narration: null,
        operator_detail: 'Delayed callback.',
        evidence_refs: [],
        operator_order_receipt: null,
      },
    ],
    latest_seq: 3,
    next_before_seq: null,
  };
}

function operationsWindow(
  newestSeq: number,
  oldestSeq: number,
  nextBeforeSeq: number | null,
): AccountEventsResponse {
  return {
    schema_version: 1,
    account_id: 'DU1234567',
    view: 'operations',
    rows: Array.from({ length: newestSeq - oldestSeq + 1 }, (_, index) => {
      const seq = newestSeq - index;
      return {
        schema_version: 1,
        event_id: `producer:DU1234567:data_plane:receipt-${seq}`,
        provenance: 'producer_operational_log' as const,
        seq,
        kind: 'safety' as const,
        occurred_at_ms: 1_780_000_000_000 + seq,
        event_at_ms: null,
        arrived_at_ms: null,
        recorded_at_ms: 1_780_000_000_000 + seq,
        producer: 'data_plane',
        producer_boot_id: 'boot-a',
        producer_seq: seq,
        trader_narration: null,
        operator_detail: `Receipt ${seq}.`,
        evidence_refs: [],
        operator_order_receipt: null,
      };
    }),
    latest_seq: newestSeq,
    next_before_seq: nextBeforeSeq,
  };
}
