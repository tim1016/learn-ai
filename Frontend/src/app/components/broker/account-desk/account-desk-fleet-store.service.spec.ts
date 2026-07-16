import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { FleetAccountSummary } from '../../../api/live-instances.types';
import { LiveRunsService } from '../../../services/live-runs.service';
import { AccountDeskFleetStore } from './account-desk-fleet-store.service';

describe('AccountDeskFleetStore', () => {
  const liveRuns = { getAccountSummary: vi.fn<() => Promise<FleetAccountSummary>>() };

  beforeEach(() => {
    liveRuns.getAccountSummary.mockReset();
    TestBed.configureTestingModule({
      providers: [
        provideZonelessChangeDetection(),
        AccountDeskFleetStore,
        { provide: LiveRunsService, useValue: liveRuns },
      ],
    });
  });

  afterEach(() => TestBed.resetTestingModule());

  it('admits only fleet evidence attested to the route and keeps last good evidence on refresh failure', async () => {
    const store = TestBed.inject(AccountDeskFleetStore);
    liveRuns.getAccountSummary.mockResolvedValue(summary('DU1234567'));
    await store.load('DU1234567');
    liveRuns.getAccountSummary.mockRejectedValueOnce(new Error('offline'));
    await store.load('DU1234567');

    expect(store.summary()?.account_id).toBe('DU1234567');
    expect(store.showingStaleLastGood()).toBe(true);
    expect(store.lastGoodAtMs()).not.toBeNull();
  });

  it('rejects a fleet response for a different account rather than reusing it', async () => {
    const store = TestBed.inject(AccountDeskFleetStore);
    liveRuns.getAccountSummary.mockResolvedValue(summary('DU7654321'));

    await store.load('DU1234567');

    expect(store.summary()).toBeNull();
    expect(store.errorMessage()).toContain('did not attest this route');
  });
});

function summary(accountId: string): FleetAccountSummary {
  return {
    account_id: accountId,
    account_identity: 'CONSISTENT',
    account_identity_reason_codes: [],
    contamination: {
      net_positions: {},
      explained_total: {},
      explained_by_instance: [],
      residual: {},
      verdict: 'clean',
      policy_blocks_starts: false,
      summary: 'Fleet is clean.',
    },
  };
}
