import { ActivatedRoute, convertToParamMap } from '@angular/router';
import { render, screen, waitFor } from '@testing-library/angular';
import { BehaviorSubject } from 'rxjs';
import { describe, expect, it, vi } from 'vitest';

import type { ClerkStatus } from '../../../api/alpaca.types';
import { BrokersService } from '../../../services/brokers.service';
import { provideFleetDirectory, testLane, TEST_ACCOUNT_ID, TEST_CLERK_ID } from '../../../fleet/fleet-directory-testing';
import { healthyAccountOperatorPostureFixture } from '../../../testing/operator-blocker-fixtures';
import { AlpacaDeskAccountDataService } from './alpaca-desk-account-data.service';
import { AlpacaHoldBannerComponent } from './alpaca-hold-banner.component';

function heldStatus(overrides: Partial<ClerkStatus> = {}): ClerkStatus {
  return {
    broker: 'alpaca',
    account_id: TEST_ACCOUNT_ID,
    hold: {
      active: true,
      reason_code: 'UNEXPLAINED_ORDER_HOLD',
      reason: 'An order this account did not submit was observed at Alpaca.',
      since_ms: 1_700_000_000_000,
    },
    latest_reconciliation: { verdict: 'unexplained_order', recorded_at_ms: 1_700_000_000_000 },
    outstanding_intents: 0,
    observed_at_ms: 1_700_000_000_000,
    operator_posture: healthyAccountOperatorPostureFixture(),
    ...overrides,
  };
}

function clearStatus(): ClerkStatus {
  return {
    broker: 'alpaca',
    account_id: TEST_ACCOUNT_ID,
    hold: { active: false, reason_code: null, reason: null, since_ms: null },
    latest_reconciliation: { verdict: 'clean', recorded_at_ms: 1_700_000_000_000 },
    outstanding_intents: 0,
    observed_at_ms: 1_700_000_000_000,
    operator_posture: healthyAccountOperatorPostureFixture(),
  };
}

/** Never resolves: this suite only cares about `clerkStatus`, and letting the
 * account resource's own `getAccount` call settle is irrelevant noise for it
 * — the same stance `alpaca-desk-account-data.service.spec.ts` takes. */
function neverAccount() {
  return vi.fn(() => new Promise<never>(() => undefined));
}

/**
 * `AlpacaHoldBannerComponent` reads `AlpacaDeskAccountDataService.clerkStatus`
 * (#2185) rather than owning its own resource and poll timer, so this suite
 * renders it the same way the account workspace does in production: with a
 * real `AlpacaDeskAccountDataService` as an ancestor provider, backed by a
 * fake `ActivatedRoute`/`BrokersService`/fleet directory.
 */
async function renderBanner(getClerkStatus: BrokersService['getClerkStatus']) {
  const paramMap$ = new BehaviorSubject(convertToParamMap({ clerkId: TEST_CLERK_ID, accountId: TEST_ACCOUNT_ID }));
  return render(AlpacaHoldBannerComponent, {
    providers: [
      provideFleetDirectory({ observed_at_ms: 1, clerks: [testLane({ clerk_id: TEST_CLERK_ID })] }),
      {
        provide: ActivatedRoute,
        useValue: { paramMap: paramMap$, snapshot: { paramMap: paramMap$.value } },
      },
      { provide: BrokersService, useValue: { getAccount: neverAccount(), getClerkStatus } },
      AlpacaDeskAccountDataService,
    ],
  });
}

describe('AlpacaHoldBannerComponent', () => {
  it('renders the hold reason_code through receiptLabel and the backend prose when held', async () => {
    await renderBanner(() => Promise.resolve(heldStatus()));

    // reason_code rendered code-like via receiptLabel (UNEXPLAINED_ORDER_HOLD →
    // "Unexplained Order Hold").
    expect(await screen.findByText(/Unexplained Order Hold/)).toBeTruthy();
    // Backend-authored reason prose is rendered unpiped, verbatim.
    expect(
      screen.getByText(/An order this account did not submit was observed at Alpaca\./),
    ).toBeTruthy();
    expect(screen.getByText(/evidence-bound recovery actions/i)).toBeTruthy();
    expect(screen.queryByRole('button', { name: /Clear hold/ })).toBeNull();
  });

  it('renders no banner when there is no active hold', async () => {
    await renderBanner(() => Promise.resolve(clearStatus()));

    // Give the resource a tick to resolve, then confirm nothing hold-related shows.
    await waitFor(() => {
      expect(screen.queryByText(/Submission paused/)).toBeNull();
    });
    expect(screen.queryByRole('button', { name: /Clear hold/ })).toBeNull();
  });

  it('renders a hold raised after a reload of the shared clerk-status resource', async () => {
    // The banner no longer owns a poll timer (#2185) — the account workspace's
    // own 15 s timer reloads `AlpacaDeskAccountDataService.clerkStatus`, and
    // this proves the banner reacts to that shared reload rather than needing
    // one of its own.
    const getClerkStatus = vi
      .fn()
      .mockResolvedValueOnce(clearStatus())
      .mockResolvedValueOnce(heldStatus());
    const view = await renderBanner(getClerkStatus);
    await waitFor(() => expect(getClerkStatus).toHaveBeenCalledTimes(1));

    const accountData = view.fixture.debugElement.injector.get(AlpacaDeskAccountDataService);
    accountData.clerkStatus.reload();

    expect(await screen.findByText(/Unexplained Order Hold/)).toBeTruthy();
  });
});
