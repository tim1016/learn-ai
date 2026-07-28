import { effect } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { describe, expect, it, vi } from 'vitest';

import { BrokerService } from '../../../services/broker.service';
import { makeAccountSafetySnapshot } from '../../../../testing/account-safety-snapshot-fixtures';
import { AccountSafetySnapshotStore } from './account-safety-snapshot.store';

function deferred<T>(): { promise: Promise<T>; resolve(value: T): void; reject(reason: unknown): void } {
  let resolvePromise!: (value: T) => void;
  let rejectPromise!: (reason: unknown) => void;
  const promise = new Promise<T>((resolve, reject) => {
    resolvePromise = resolve;
    rejectPromise = reject;
  });
  return { promise, resolve: resolvePromise, reject: rejectPromise };
}

describe('AccountSafetySnapshotStore', () => {
  it('keeps a newer snapshot when an older request arrives out of order', async () => {
    const first = deferred<ReturnType<typeof makeAccountSafetySnapshot>>();
    const second = deferred<ReturnType<typeof makeAccountSafetySnapshot>>();
    const broker = { accountSafetySnapshot: vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise) };
    TestBed.configureTestingModule({ providers: [{ provide: BrokerService, useValue: broker }] });
    const store = TestBed.inject(AccountSafetySnapshotStore);

    const olderRequest = store.refresh('DU1234567');
    const newerRequest = store.refresh('DU1234567');
    second.resolve(makeAccountSafetySnapshot({ generated_at_ms: 200, snapshot_version: 'c'.repeat(64) }));
    await newerRequest;
    first.resolve(makeAccountSafetySnapshot({ generated_at_ms: 100, snapshot_version: 'd'.repeat(64) }));
    await olderRequest;

    expect(store.stateFor('DU1234567').snapshot?.snapshot_version).toBe('c'.repeat(64));
  });

  it('preserves last-good server evidence as stale after a refresh outage', async () => {
    const broker = {
      accountSafetySnapshot: vi.fn()
        .mockResolvedValueOnce(makeAccountSafetySnapshot())
        .mockRejectedValueOnce(new Error('offline')),
    };
    TestBed.configureTestingModule({ providers: [{ provide: BrokerService, useValue: broker }] });
    const store = TestBed.inject(AccountSafetySnapshotStore);

    await store.refresh('DU1234567');
    await store.refresh('DU1234567');

    expect(store.stateFor('DU1234567')).toMatchObject({
      state: 'stale',
      snapshot: { verdict: 'RECONCILING' },
    });
  });

  it('accepts the latest server version even when two snapshots share a millisecond timestamp', async () => {
    const broker = {
      accountSafetySnapshot: vi.fn()
        .mockResolvedValueOnce(
          makeAccountSafetySnapshot({ generated_at_ms: 200, snapshot_version: 'a'.repeat(64) }),
        )
        .mockResolvedValueOnce(
          makeAccountSafetySnapshot({ generated_at_ms: 200, snapshot_version: 'b'.repeat(64) }),
        ),
    };
    TestBed.configureTestingModule({ providers: [{ provide: BrokerService, useValue: broker }] });
    const store = TestBed.inject(AccountSafetySnapshotStore);

    await store.refresh('DU1234567');
    await store.refresh('DU1234567');

    expect(store.stateFor('DU1234567').snapshot?.snapshot_version).toBe('b'.repeat(64));
  });

  it('reports unavailable rather than inventing a clean verdict when the first fetch fails', async () => {
    const broker = { accountSafetySnapshot: vi.fn().mockRejectedValue(new Error('offline')) };
    TestBed.configureTestingModule({ providers: [{ provide: BrokerService, useValue: broker }] });
    const store = TestBed.inject(AccountSafetySnapshotStore);

    await store.refresh('DU1234567');

    expect(store.stateFor('DU1234567')).toEqual({ state: 'unavailable', snapshot: null });
  });

  it('fails closed when a response belongs to a different account', async () => {
    const broker = {
      accountSafetySnapshot: vi.fn().mockResolvedValue(makeAccountSafetySnapshot({ account_id: 'DU7654321' })),
    };
    TestBed.configureTestingModule({ providers: [{ provide: BrokerService, useValue: broker }] });
    const store = TestBed.inject(AccountSafetySnapshotStore);

    await store.refresh('DU1234567');

    expect(store.stateFor('DU1234567')).toEqual({ state: 'unavailable', snapshot: null });
  });

  it('does not self-trigger when a host refreshes inside an Angular effect', async () => {
    const broker = {
      accountSafetySnapshot: vi.fn().mockResolvedValue(makeAccountSafetySnapshot()),
    };
    TestBed.configureTestingModule({ providers: [{ provide: BrokerService, useValue: broker }] });
    const store = TestBed.inject(AccountSafetySnapshotStore);
    const refreshEffect = TestBed.runInInjectionContext(() =>
      effect(() => {
        void store.refresh('DU1234567');
      }),
    );

    TestBed.flushEffects();
    await Promise.resolve();
    TestBed.flushEffects();

    expect(broker.accountSafetySnapshot).toHaveBeenCalledTimes(1);
    refreshEffect.destroy();
  });
});
