import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { AccountServiceStatusResponse, AccountsRosterResponse } from '../../../api/account-directory.types';
import { BrokerService } from '../../../services/broker.service';
import { AccountDeskDirectoryStore } from './account-desk-directory-store.service';

describe('AccountDeskDirectoryStore', () => {
  const broker = {
    accounts: vi.fn<() => Promise<AccountsRosterResponse>>(),
    accountServiceStatus: vi.fn<(accountId: string) => Promise<AccountServiceStatusResponse>>(),
  };

  beforeEach(() => {
    broker.accounts.mockReset();
    broker.accountServiceStatus.mockReset();
    TestBed.configureTestingModule({
      providers: [
        provideZonelessChangeDetection(),
        AccountDeskDirectoryStore,
        { provide: BrokerService, useValue: broker },
      ],
    });
  });

  afterEach(() => TestBed.resetTestingModule());

  it('keeps the last good roster visible when a refresh fails and supports retry', async () => {
    broker.accounts.mockResolvedValue(roster());
    const store = TestBed.inject(AccountDeskDirectoryStore);

    await store.loadRoster();
    broker.accounts.mockRejectedValueOnce(new Error('offline'));
    await store.loadRoster();

    expect(store.rosterRows()).toHaveLength(1);
    expect(store.rosterShowingStaleLastGood()).toBe(true);
    expect(store.rosterErrorMessage()).toBe('Account roster is unavailable. Retry to request it again.');
  });

  it('clears old Account service data and ignores an old-account response after a route change', async () => {
    const first = deferred<AccountServiceStatusResponse>();
    broker.accountServiceStatus.mockImplementationOnce(() => first.promise).mockResolvedValueOnce(status('DU7654321'));
    const store = TestBed.inject(AccountDeskDirectoryStore);

    const firstLoad = store.loadServiceStatus('DU1234567');
    await Promise.resolve();
    const secondLoad = store.loadServiceStatus('DU7654321');
    first.resolve(status('DU1234567'));
    await Promise.all([firstLoad, secondLoad]);

    expect(store.statusAccountId()).toBe('DU7654321');
    expect(store.serviceStatus()?.account_id).toBe('DU7654321');
  });

  it('keeps the initial error distinct from an empty configured-account roster', async () => {
    const store = TestBed.inject(AccountDeskDirectoryStore);
    broker.accounts.mockRejectedValueOnce(new Error('offline'));

    await store.loadRoster();

    expect(store.rosterHasLastGood()).toBe(false);
    expect(store.rosterEmpty()).toBe(false);
    expect(store.rosterErrorMessage()).toBe('Account roster is unavailable. Retry to request it again.');
  });
});

function roster(): AccountsRosterResponse {
  return {
    schema_version: 1,
    rows: [{
      account_id: 'DU1234567',
      broker: 'IBKR',
      effective_posture: 'PAPER_EXECUTION',
      service: { attachment: 'UNATTACHED', phase: null, generation: null },
      latest_verdict_summary: { state: 'NOT_PROVEN', headline: 'Verification is required.', generated_at_ms: 1_780_000_000_000 },
      last_verified_at_ms: null,
    }],
  };
}

function status(accountId: string): AccountServiceStatusResponse {
  return {
    schema_version: 1,
    account_id: accountId,
    attachment: 'UNATTACHED',
    phase: null,
    generation: null,
    generation_recorded_at_ms: null,
    source: null,
    binding: { state: 'UNATTACHED', generation: null, lease_generation: null },
    lease: null,
    journal: { last_seq: null, last_write_ms: null },
  };
}

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}
