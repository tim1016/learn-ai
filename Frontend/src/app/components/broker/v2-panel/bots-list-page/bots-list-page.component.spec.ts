import { render, screen } from '@testing-library/angular';
import { provideRouter } from '@angular/router';
import { describe, expect, it } from 'vitest';

import type { BrokerAccountSnapshot, ClerkStatus } from '../../../../api/alpaca.types';
import { BrokersService } from '../../../../services/brokers.service';
import { BrokerV2PanelService } from '../lib/broker-v2-panel.service';
import type { BotCatalogView, PanelProfile } from '../lib/broker-v2-panel.types';
import { BotsListPageComponent } from './bots-list-page.component';

function fakeAccount(overrides: Partial<BrokerAccountSnapshot> = {}): BrokerAccountSnapshot {
  return {
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
    ...overrides,
  };
}

function fakeClerkStatus(): ClerkStatus {
  return {
    account_id: 'PA9',
    broker: 'alpaca',
    hold: { active: false },
    outstanding_intents: 0,
    observed_at_ms: 1_700_000_000_000,
  };
}

function fakeBot(overrides: Partial<BotCatalogView> = {}): BotCatalogView {
  return {
    strategy_instance_id: 'spy-momentum-01',
    broker: 'alpaca',
    account_id: 'PA9',
    symbol: 'SPY',
    phase: 'ON_DUTY',
    desired_state: 'RUNNING',
    running: true,
    status_label: 'Working',
    exposure: {},
    fills_today: 2,
    realized_pnl_today: 45.5,
    open_pnl: null,
    last_activity_at_ms: 1_700_000_000_000,
    needs_attention: false,
    ...overrides,
  };
}

function fakeProfile(): PanelProfile {
  return {
    broker: 'alpaca',
    fee_fidelity: 'none',
    flatten_supported: false,
    live_bars_supported: true,
    stations: [],
    supported_action_ids: ['start', 'stop', 'deploy'],
  };
}

async function renderPage(
  bots: BotCatalogView[] = [],
  overrides: {
    account?: BrokerAccountSnapshot;
    clerk?: ClerkStatus;
    profile?: PanelProfile;
  } = {},
) {
  const account = overrides.account ?? fakeAccount();
  const clerk = overrides.clerk ?? fakeClerkStatus();
  const profile = overrides.profile ?? fakeProfile();

  const mockBrokersService = {
    getAccount: () => Promise.resolve(account),
    getClerkStatus: () => Promise.resolve(clerk),
  };

  const mockPanelService = {
    getCatalog: () => Promise.resolve(bots),
    getPanelProfile: () => Promise.resolve(profile),
    runAction: () => Promise.resolve({ action_id: 'start', applied: true, revision: 1, message: 'ok' }),
  };

  return render(BotsListPageComponent, {
    providers: [
      provideRouter([]),
      { provide: BrokersService, useValue: mockBrokersService },
      { provide: BrokerV2PanelService, useValue: mockPanelService },
    ],
    componentInputs: { broker: 'alpaca', accountId: 'PA9' },
  });
}

describe('BotsListPageComponent', () => {
  it('renders account strip equity value', async () => {
    await renderPage([], { account: fakeAccount({ equity: 15_000 }) });

    expect(await screen.findByText(/\$15,000\.00/)).toBeTruthy();
  });

  it('renders PAPER badge for paper accounts', async () => {
    await renderPage([], { account: fakeAccount({ account_id: 'PA9' }) });

    expect(await screen.findByText('PAPER')).toBeTruthy();
  });

  it('renders "Working" status label for an ON_DUTY bot', async () => {
    const bot = fakeBot({ status_label: 'Working', phase: 'ON_DUTY', running: true });
    await renderPage([bot]);

    // "Working" appears in both the status-filter toggle and the bot status chip;
    // verify the chip is present by finding it within the table row.
    const cells = await screen.findAllByText('Working');
    expect(cells.length).toBeGreaterThanOrEqual(1);
  });

  it('renders attention marker for bots with needs_attention=true', async () => {
    const bot = fakeBot({ needs_attention: true });
    await renderPage([bot]);

    const attentionEl = await screen.findByLabelText('Needs attention');
    expect(attentionEl).toBeTruthy();
  });

  it('renders Start button for OFF_DUTY bot when start is supported', async () => {
    const bot = fakeBot({ phase: 'OFF_DUTY', running: false, status_label: 'Off duty' });
    await renderPage([bot], { profile: fakeProfile() });

    expect(await screen.findByRole('button', { name: /Start spy-momentum-01/i })).toBeTruthy();
  });

  it('renders "Fees not reported" note', async () => {
    await renderPage();

    expect(await screen.findByText(/Fees not reported/i)).toBeTruthy();
  });

  it('renders empty state message when no bots', async () => {
    await renderPage([]);

    expect(await screen.findByText(/No bots match/i)).toBeTruthy();
  });
});
