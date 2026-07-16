import { ActivatedRoute, Router } from '@angular/router';
import { render, screen, waitFor } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type { AccountsRosterResponse } from '../../../api/account-directory.types';
import { BrokerService } from '../../../services/broker.service';
import { AccountMonitorRedirectComponent } from './account-monitor-redirect.component';

async function setup(
  roster: Promise<AccountsRosterResponse>,
  fragment: string | null = null,
) {
  const broker = { accounts: vi.fn().mockReturnValue(roster) };
  const router = { navigate: vi.fn().mockResolvedValue(true) };
  await render(AccountMonitorRedirectComponent, {
    providers: [
      { provide: BrokerService, useValue: broker },
      { provide: Router, useValue: router },
      { provide: ActivatedRoute, useValue: { snapshot: { fragment } } },
    ],
  });
  return { broker, router };
}

describe('AccountMonitorRedirectComponent', () => {
  it('opens the only account and maps a known legacy fragment to an operations anchor', async () => {
    const { router } = await setup(Promise.resolve(roster(['DU1234567'])), 'account-reconciliation-action');

    expect(screen.getByText('Opening Accounts…')).toBeTruthy();
    await waitFor(() => expect(router.navigate).toHaveBeenCalledWith(
      ['/broker/accounts', 'DU1234567'],
      { fragment: 'account-desk-recovery-controls', replaceUrl: true },
    ));
  });

  it.each([
    { accountIds: [] as readonly string[] },
    { accountIds: ['DU1234567', 'DU7654321'] as readonly string[] },
  ])(
    'lands on the roster when the legacy route cannot select one account',
    async ({ accountIds }) => {
      const { router } = await setup(Promise.resolve(roster(accountIds)), 'account-reconciliation-action');

      await waitFor(() => expect(router.navigate).toHaveBeenCalledWith(
        ['/broker/accounts'],
        { replaceUrl: true },
      ));
    },
  );

  it('lands safely on the one-account desk for an unknown fragment', async () => {
    const { router } = await setup(Promise.resolve(roster(['DU1234567'])), 'unknown-anchor');
    await waitFor(() => expect(router.navigate).toHaveBeenCalledWith(
      ['/broker/accounts', 'DU1234567'],
      { replaceUrl: true },
    ));
  });

  it('lands safely on the roster when the roster is unavailable', async () => {
    const unavailable = new Promise<AccountsRosterResponse>((_, reject) => {
      queueMicrotask(() => reject(new Error('offline')));
    });
    const { router } = await setup(unavailable);
    await waitFor(() => expect(router.navigate).toHaveBeenCalledWith(
      ['/broker/accounts'],
      { replaceUrl: true },
    ));
  });
});

function roster(accountIds: readonly string[]): AccountsRosterResponse {
  return {
    schema_version: 2,
    rows: accountIds.map((account_id) => ({
      account_id,
      broker: 'IBKR',
      effective_posture: 'UNKNOWN',
      service: {
        attachment: 'UNATTACHED', phase: null, generation: null,
        operating_state: 'ATTENTION', headline: 'Account service needs attention',
      },
      latest_verdict_summary: {
        state: 'NOT_PROVEN',
        headline: 'No live account observation is available.',
        generated_at_ms: 1_780_000_000_000,
      },
      last_verified_at_ms: null,
    })),
  };
}
