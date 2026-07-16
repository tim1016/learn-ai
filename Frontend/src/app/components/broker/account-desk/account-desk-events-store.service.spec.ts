import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { AccountEventsRequest, AccountEventsResponse } from '../../../api/account-events.types';
import { BrokerService } from '../../../services/broker.service';
import { AccountDeskEventsStore } from './account-desk-events-store.service';

describe('AccountDeskEventsStore', () => {
  const broker = { accountEvents: vi.fn() };

  beforeEach(() => {
    broker.accountEvents.mockReset();
    TestBed.configureTestingModule({
      providers: [
        provideZonelessChangeDetection(),
        AccountDeskEventsStore,
        { provide: BrokerService, useValue: broker },
      ],
    });
  });

  afterEach(() => TestBed.resetTestingModule());

  it('keeps trader narration separate from the full operations history and requests each view', async () => {
    broker.accountEvents.mockImplementation((_accountId: string, request: AccountEventsRequest) =>
      Promise.resolve(request.view === 'trader_today' ? traderPage() : operationsPage()),
    );
    const store = TestBed.inject(AccountDeskEventsStore);

    await store.load('DU1234567');

    expect(store.traderRows().map((row) => row.event_id)).toEqual(['DU1234567:2']);
    expect(store.operationRows().map((row) => row.event_id)).toEqual(['DU1234567:2', 'DU1234567:1']);
    expect(broker.accountEvents).toHaveBeenCalledWith('DU1234567', expect.objectContaining({ view: 'trader_today' }));
    expect(broker.accountEvents).toHaveBeenCalledWith('DU1234567', expect.objectContaining({ view: 'operations' }));
  });

  it('deduplicates a newer polling page by stable event identity while retaining history', async () => {
    broker.accountEvents
      .mockImplementationOnce((_accountId: string, request: AccountEventsRequest) =>
        Promise.resolve(request.view === 'trader_today' ? traderPage() : operationsPage()),
      )
      .mockImplementationOnce((_accountId: string, request: AccountEventsRequest) =>
        Promise.resolve(request.view === 'trader_today' ? traderPage() : operationsPage()),
      )
      .mockImplementationOnce((_accountId: string, request: AccountEventsRequest) =>
        Promise.resolve(request.view === 'trader_today' ? traderPage([traderRow(3), traderRow(2)], 3) : operationsPage()),
      )
      .mockImplementationOnce((_accountId: string, request: AccountEventsRequest) =>
        Promise.resolve(request.view === 'trader_today' ? traderPage() : operationsPage([traderRow(3), operatorRow(2)], 3)),
      );
    const store = TestBed.inject(AccountDeskEventsStore);

    await store.load('DU1234567');
    await store.load('DU1234567');

    expect(store.traderRows().map((row) => row.event_id)).toEqual(['DU1234567:3', 'DU1234567:2']);
    expect(store.operationRows().map((row) => row.event_id)).toEqual(['DU1234567:3', 'DU1234567:2', 'DU1234567:1']);
    expect(broker.accountEvents).toHaveBeenLastCalledWith(
      'DU1234567',
      expect.objectContaining({ view: 'operations', afterSeq: 2 }),
    );
  });

  it('keeps same-account last-good history and a retryable stale state after refresh errors', async () => {
    broker.accountEvents
      .mockImplementationOnce((_accountId: string, request: AccountEventsRequest) =>
        Promise.resolve(request.view === 'trader_today' ? traderPage() : operationsPage()),
      )
      .mockImplementationOnce((_accountId: string, request: AccountEventsRequest) =>
        Promise.resolve(request.view === 'trader_today' ? traderPage() : operationsPage()),
      )
      .mockRejectedValueOnce(new Error('offline'))
      .mockRejectedValueOnce(new Error('offline'));
    const store = TestBed.inject(AccountDeskEventsStore);

    await store.load('DU1234567');
    await store.load('DU1234567');

    expect(store.traderRows()).toHaveLength(1);
    expect(store.operationRows()).toHaveLength(2);
    expect(store.traderShowingStaleLastGood()).toBe(true);
    expect(store.operationsShowingStaleLastGood()).toBe(true);
  });

  it('clears previous account history rather than blending event identities across routes', async () => {
    broker.accountEvents.mockImplementation((accountId: string, request: AccountEventsRequest) => {
      const rows = accountId === 'DU7654321' ? [traderRow(1, 'DU7654321')] : [traderRow(2)];
      return Promise.resolve(page(request.view, accountId, rows, 1));
    });
    const store = TestBed.inject(AccountDeskEventsStore);

    await store.load('DU1234567');
    await store.load('DU7654321');

    expect(store.traderRows().map((row) => row.event_id)).toEqual(['DU7654321:1']);
    expect(store.operationRows().map((row) => row.event_id)).toEqual(['DU7654321:1']);
  });

  it('ignores an in-flight timeline response after its kind filter changes', async () => {
    const staleOperations = deferred<AccountEventsResponse>();
    broker.accountEvents.mockImplementation((_accountId: string, request: AccountEventsRequest) => {
      if (request.view === 'trader_today') return Promise.resolve(traderPage());
      if (!request.kinds?.length) return staleOperations.promise;
      return Promise.resolve(operationsPage([operatorRow(2)], 2));
    });
    const store = TestBed.inject(AccountDeskEventsStore);

    const initialLoad = store.load('DU1234567');
    await Promise.resolve();
    store.toggleOperationKind('clerk');
    staleOperations.resolve(operationsPage([traderRow(9)], 9));
    await initialLoad;
    await Promise.resolve();

    expect(store.operationKinds()).toEqual(['clerk']);
    expect(store.operationRows().map((row) => row.event_id)).toEqual(['DU1234567:2']);
  });
});

function traderPage(rows = [traderRow(2)], latestSeq = 2): AccountEventsResponse {
  return page('trader_today', 'DU1234567', rows, latestSeq);
}

function operationsPage(rows = [operatorRow(2), operatorRow(1)], latestSeq = 2): AccountEventsResponse {
  return page('operations', 'DU1234567', rows, latestSeq, 1);
}

function page(
  view: AccountEventsResponse['view'],
  accountId: string,
  rows: AccountEventsResponse['rows'],
  latestSeq: number,
  nextBeforeSeq: number | null = null,
): AccountEventsResponse {
  return {
    schema_version: 1,
    account_id: accountId,
    view,
    rows,
    latest_seq: latestSeq,
    next_before_seq: nextBeforeSeq,
  };
}

function traderRow(seq: number, accountId = 'DU1234567'): AccountEventsResponse['rows'][number] {
  return {
    schema_version: 1,
    event_id: `${accountId}:${seq}`,
    seq,
    kind: 'safety',
    occurred_at_ms: 1_780_000_000_000 + seq,
    trader_narration: `Backend narration ${seq}.`,
    operator_detail: `Operator detail ${seq}.`,
    evidence_refs: [{ source: 'account_event_journal', ref: `${accountId}:${seq}`, detail: null }],
  };
}

function operatorRow(seq: number): AccountEventsResponse['rows'][number] {
  return {
    ...traderRow(seq),
    kind: 'clerk',
    trader_narration: null,
  };
}

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}
