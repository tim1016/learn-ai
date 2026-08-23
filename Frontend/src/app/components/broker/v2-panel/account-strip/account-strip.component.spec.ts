import { fireEvent, render, screen, within } from '@testing-library/angular';
import { provideRouter, Router } from '@angular/router';
import { describe, expect, it, vi } from 'vitest';

import type { BrokerAccountSnapshot, ClerkStatus } from '../../../../api/alpaca.types';
import type { AccountOperatorPosture } from '../../../../api/operator-blocker.types';
import { operatorBlockerFixture } from '../../../../testing/operator-blocker-fixtures';
import { AccountStripComponent } from './account-strip.component';

const account: BrokerAccountSnapshot = {
  broker: 'alpaca',
  account_id: 'PA9',
  account_mode: 'paper',
  account_status: 'ACTIVE',
  currency: 'USD',
  cash: 10_000,
  equity: 15_000,
  buying_power: 30_000,
  portfolio_value: 15_000,
  long_market_value: 5_000,
  short_market_value: 0,
  pattern_day_trader: false,
  trading_blocked: false,
  account_blocked: false,
  created_at_ms: 1_600_000_000_000,
  observed_at_ms: 1_700_000_000_000,
};

const healthyPosture: AccountOperatorPosture = {
  condition: null,
  account_desk: null,
  fleet_roster: null,
  status_headline: 'Account Clerk custody is healthy',
  status_detail: 'Durable Clerk state has no active hold or unresolved uncertainty in this scope.',
};

function blockedPosture(
  overrides: Parameters<typeof operatorBlockerFixture>[0] = {},
): AccountOperatorPosture {
  const blocker = operatorBlockerFixture({ host: 'fleet_roster', ...overrides });
  return {
    condition: blocker.condition,
    account_desk: { ...blocker, host: 'account_desk' },
    fleet_roster: blocker,
    status_headline: blocker.headline,
    status_detail: blocker.detail ?? null,
  };
}

const clerkStatus: ClerkStatus = {
  account_id: 'PA9',
  broker: 'alpaca',
  hold: { active: false },
  outstanding_intents: 0,
  observed_at_ms: 1_700_000_000_000,
  latest_reconciliation: {
    recorded_at_ms: 1_700_000_000_000,
    verdict: 'clean',
  },
  channel_healths: [
    { stream: 'market_data', healthy: true, observed_at_ms: 1_700_000_000_000 },
    { stream: 'execution', healthy: false, observed_at_ms: 1_700_000_000_000 },
  ],
  operator_posture: healthyPosture,
};

describe('AccountStripComponent', () => {
  it('renders an explicit loading state before account posture is known', async () => {
    await render(AccountStripComponent, { componentInputs: { loading: true } });

    expect(screen.getByRole('status').textContent).toContain('Resolving paper account');
  });

  it('keeps an account lookup failure explicit without claiming the fleet is hidden', async () => {
    await render(AccountStripComponent, {
      componentInputs: { accountUnavailable: true },
    });

    const alert = screen.getByRole('alert');
    expect(alert.textContent).toContain('Account confirmation unavailable');
    expect(alert.textContent).toContain('The roster can still refresh');
    expect(alert.textContent).not.toContain('fleet remains hidden');
  });

  it('renders the backend-authored fleet_roster status headline, not a client-derived verdict', async () => {
    await render(AccountStripComponent, {
      componentInputs: { account, clerkStatus },
    });

    const posture = screen.getByLabelText('Alpaca account posture');
    expect(within(posture).getByText('Account Clerk custody is healthy')).toBeTruthy();
    expect(within(posture).getByText('Clean')).toBeTruthy();
    expect(within(posture).getByText(/Market Data healthy/i)).toBeTruthy();
    expect(within(posture).getByText(/Execution unhealthy/i)).toBeTruthy();
    expect(within(posture).queryByText('Trading available')).toBeNull();
    expect(within(posture).queryByText('Trading blocked')).toBeNull();
  });

  it('renders the fleet_roster blocker headline/detail when a condition is active', async () => {
    await render(AccountStripComponent, {
      componentInputs: {
        account,
        clerkStatus: {
          ...clerkStatus,
          operator_posture: blockedPosture({
            disposition: 'wait',
            primaryMove: null,
            headline: 'An account command is still resolving',
            detail: '2 account command(s) are still awaiting a final broker outcome.',
          }),
        },
      },
    });

    const posture = screen.getByLabelText('Alpaca account posture');
    expect(within(posture).getByText('An account command is still resolving')).toBeTruthy();
    expect(
      within(posture).getByText('2 account command(s) are still awaiting a final broker outcome.'),
    ).toBeTruthy();
  });

  it('renders and dispatches the fleet_roster projection\'s declared navigate move', async () => {
    const view = await render(AccountStripComponent, {
      componentInputs: {
        account,
        clerkStatus: {
          ...clerkStatus,
          operator_posture: blockedPosture({
            disposition: 'fix_elsewhere',
            headline: 'Clerk reconciliation required',
          }),
        },
      },
      providers: [provideRouter([])],
    });

    const router = view.fixture.debugElement.injector.get(Router);
    const navigate = vi.spyOn(router, 'navigate').mockResolvedValue(true);

    const posture = screen.getByLabelText('Alpaca account posture');
    fireEvent.click(within(posture).getByRole('button', { name: 'Account detail' }));
    fireEvent.click(within(posture).getByRole('button', { name: 'Connect the broker' }));

    expect(navigate).toHaveBeenCalledWith(['/broker'], { fragment: undefined });
  });

  it('does not render a button for a move it cannot dispatch itself', async () => {
    await render(AccountStripComponent, {
      componentInputs: {
        account,
        clerkStatus: {
          ...clerkStatus,
          operator_posture: blockedPosture({
            disposition: 'fix_here',
            headline: 'Clerk reconciliation required',
            primaryMove: {
              label: 'Open Clerk recovery',
              action: { kind: 'confirm_in_form', anchor: 'account-desk-clerk-recovery' },
              target: null,
            },
          }),
        },
      },
    });

    const posture = screen.getByLabelText('Alpaca account posture');
    fireEvent.click(within(posture).getByRole('button', { name: 'Account detail' }));

    // Only the disclosure toggle itself remains: the move is not self-dispatchable.
    expect(
      within(posture)
        .getAllByRole('button')
        .map((button) => button.textContent?.trim()),
    ).toEqual(['Hide account detail']);
  });

  it('fails closed to Loading rather than re-deriving a verdict when posture has not loaded', async () => {
    await render(AccountStripComponent, {
      componentInputs: { account },
    });

    expect(screen.getByText('Loading')).toBeTruthy();
    expect(screen.getByText('Custody not observed')).toBeTruthy();
    expect(screen.queryByText('Account Clerk custody is healthy')).toBeNull();
  });

  it('renders unavailable custody without a last-good Clerk snapshot', async () => {
    await render(AccountStripComponent, {
      componentInputs: { account, clerkUnavailable: true },
    });

    fireEvent.click(screen.getByRole('button', { name: 'Account detail' }));

    expect(screen.getAllByText('Unavailable')).toHaveLength(2);
    expect(screen.getByText('Custody not observed')).toBeTruthy();
    expect(screen.getByText('Reconciliation unavailable')).toBeTruthy();
  });

  it('preserves backend-authored hold prose in an alert', async () => {
    await render(AccountStripComponent, {
      componentInputs: {
        account,
        clerkStatus: {
          ...clerkStatus,
          hold: {
            active: true,
            reason_code: 'BROKER_POSITION_UNATTRIBUTED',
            reason: 'One broker position is not attributed to a strategy.',
          },
        },
      },
    });

    const alert = screen.getByRole('alert');
    expect(alert.textContent?.toLowerCase()).toContain('broker position unattributed');
    expect(alert.textContent).toContain('One broker position is not attributed to a strategy.');
  });

  it('labels a last-good posture when a refresh fails', async () => {
    await render(AccountStripComponent, {
      componentInputs: { account, clerkStatus, accountUnavailable: true },
    });

    expect(screen.getByRole('status').textContent).toContain(
      'Showing the last broker observation',
    );
  });

  it('renders account freeze ahead of an active hold', async () => {
    await render(AccountStripComponent, {
      componentInputs: {
        account,
        clerkStatus: {
          ...clerkStatus,
          freeze: {
            active: true,
            category: 'ACCOUNT_STATE_UNPROVABLE',
            explanation: 'Current custody cannot be proved.',
            next_step: 'Run reconciliation.',
            observed_at_ms: 1_700_000_000_000,
          },
          hold: {
            active: true,
            reason_code: 'BROKER_POSITION_UNATTRIBUTED',
            reason: 'This hold must remain hidden behind the freeze.',
          },
        },
      },
    });

    const alert = screen.getByRole('alert');
    expect(alert.textContent).toContain('Account frozen');
    expect(alert.textContent).toContain('Current custody cannot be proved.');
    expect(screen.queryByText('This hold must remain hidden behind the freeze.')).toBeNull();
  });
});
