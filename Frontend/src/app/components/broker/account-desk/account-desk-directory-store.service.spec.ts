import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { AccountServiceStatusResponse, AccountsRosterResponse } from '../../../api/account-directory.types';
import type { AccountCockpitResponse } from '../../../api/account-cockpit.types';
import { BrokerService } from '../../../services/broker.service';
import { AccountDeskDirectoryStore } from './account-desk-directory-store.service';

describe('AccountDeskDirectoryStore', () => {
  const broker = {
    accounts: vi.fn<() => Promise<AccountsRosterResponse>>(),
    accountCockpit: vi.fn<(accountId: string) => Promise<AccountCockpitResponse>>(),
  };

  beforeEach(() => {
    broker.accounts.mockReset();
    broker.accountCockpit.mockReset();
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
    const first = deferred<AccountCockpitResponse>();
    broker.accountCockpit.mockImplementationOnce(() => first.promise).mockResolvedValueOnce(cockpit('DU7654321'));
    const store = TestBed.inject(AccountDeskDirectoryStore);

    const firstLoad = store.loadServiceStatus('DU1234567');
    await Promise.resolve();
    const secondLoad = store.loadServiceStatus('DU7654321');
    first.resolve(cockpit('DU1234567'));
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

  it('preserves a plain FastAPI error detail instead of hiding it behind the roster fallback', async () => {
    const store = TestBed.inject(AccountDeskDirectoryStore);
    broker.accounts.mockRejectedValueOnce({ error: { detail: 'The configured account roster is unavailable.' } });

    await store.loadRoster();

    expect(store.rosterErrorMessage()).toBe('The configured account roster is unavailable.');
  });
});

function roster(): AccountsRosterResponse {
  return {
    schema_version: 2,
    rows: [{
      account_id: 'DU1234567',
      broker: 'IBKR',
      effective_posture: 'PAPER_EXECUTION',
      service: {
        attachment: 'UNATTACHED', phase: null, generation: null,
        operating_state: 'ATTENTION', headline: 'Account service needs attention',
      },
      latest_verdict_summary: { state: 'NOT_PROVEN', headline: 'Verification is required.', generated_at_ms: 1_780_000_000_000 },
      last_verified_at_ms: null,
    }],
  };
}

function status(accountId: string): AccountServiceStatusResponse {
  return {
    schema_version: 3,
    account_id: accountId,
    attachment: 'UNATTACHED',
    phase: null,
    generation: null,
    generation_recorded_at_ms: null,
    source: null,
    binding: {
      state: 'UNATTACHED',
      generation: null,
      lease_generation: null,
      pending_retirement_proposals: 0,
      ledger_read_authority: 'legacy_registry',
      ledger_parity: 'clean',
      ledger_parity_issue_count: 0,
    },
    gate_authority: {
      requested_authority: 'account_truth', effective_authority: 'account_truth', promotion_state: 'SAFE_DEFAULT',
      reason_code: 'ACCOUNT_GATE_SAFE_DEFAULT', disposition: null, action_authority: 'account_truth',
      action_gate: {
        gate_id: 'account.account_truth', status: 'block', source: 'test', operator_reason: 'ACCOUNT_TRUTH_NOT_AVAILABLE',
        operator_next_step: 'Refresh account truth.', evidence_at_ms: 1_780_000_000_000,
      }, observed_session_dates: [], lease_weaker_comparison_count: 0, restart_smoke_recorded_at_ms: null,
    },
    session_policy: {
      allow_outside_live_session: false,
      gate_result: {
        gate_id: 'account.live_session', status: 'block', source: 'test', operator_reason: 'OUTSIDE_LIVE_TRADABLE_SESSION',
        operator_next_step: 'Wait for a session.', evidence_at_ms: 1_780_000_000_000,
      },
    },
    lease: null,
    journal: { last_seq: null, last_write_ms: null },
    operating_state: 'ATTENTION',
    headline: 'Account service needs attention',
    detail: 'Account verification cannot stay current until the account service is attached.',
  };
}

function cockpit(accountId: string): AccountCockpitResponse {
  return {
    schema_version: 1,
    account_id: accountId,
    generated_at_ms: 1_780_000_000_000,
    mode: 'NORMAL',
    clerk: status(accountId),
    daemon: {
      availability: 'AVAILABLE', reason_code: 'DAEMON_CONNECTED',
      detail: 'The host daemon is reachable.', observed_at_ms: 1_780_000_000_000,
    },
    blockers: [],
  };
}

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}
