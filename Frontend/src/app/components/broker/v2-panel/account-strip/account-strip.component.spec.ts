import { render, screen, within } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import type { BrokerAccountSnapshot, ClerkStatus } from '../../../../api/alpaca.types';
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
};

describe('AccountStripComponent', () => {
  it('renders an explicit loading state before account posture is known', async () => {
    await render(AccountStripComponent, { componentInputs: { loading: true } });

    expect(screen.getByRole('status').textContent).toContain('Resolving paper account');
  });

  it('renders reconciliation and channel health as text, not color alone', async () => {
    await render(AccountStripComponent, {
      componentInputs: { account, clerkStatus },
    });

    const posture = screen.getByLabelText('Alpaca account posture');
    expect(within(posture).getByText('Clean')).toBeTruthy();
    expect(within(posture).getByText(/Market Data healthy/i)).toBeTruthy();
    expect(within(posture).getByText(/Execution unhealthy/i)).toBeTruthy();
  });

  it('does not claim custody is clear before Clerk posture is observed', async () => {
    await render(AccountStripComponent, {
      componentInputs: { account },
    });

    expect(screen.getByText('Loading')).toBeTruthy();
    expect(screen.getByText('Custody not observed')).toBeTruthy();
    expect(screen.queryByText('Clear')).toBeNull();
    expect(screen.queryByText('No custody block')).toBeNull();
  });

  it('renders unavailable custody without a last-good Clerk snapshot', async () => {
    await render(AccountStripComponent, {
      componentInputs: { account, clerkUnavailable: true },
    });

    expect(screen.getAllByText('Unavailable')).toHaveLength(3);
    expect(screen.getByText('Custody not observed')).toBeTruthy();
    expect(screen.queryByText('Clear')).toBeNull();
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
    expect(screen.getByText('Frozen')).toBeTruthy();
    expect(screen.queryByText('This hold must remain hidden behind the freeze.')).toBeNull();
  });
});
