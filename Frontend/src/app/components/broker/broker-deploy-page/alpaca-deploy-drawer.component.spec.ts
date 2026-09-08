import { provideRouter } from '@angular/router';
import { render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type { BrokerAccountSnapshot } from '../../../api/alpaca.types';
import { BrokersService } from '../../../services/brokers.service';
import { BrokerV2PanelService } from '../v2-panel/lib/broker-v2-panel.service';
import { AlpacaDeployDrawerComponent } from './alpaca-deploy-drawer.component';
import { DEPLOY_VIEW, SHADOW_DEPLOY_VIEW } from './alpaca-deploy-workflow.fixtures';

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

async function renderDrawer(
  account: BrokerAccountSnapshot | Error,
  view = DEPLOY_VIEW,
) {
  const brokers = {
    getAccount: account instanceof Error
      ? vi.fn().mockRejectedValue(account)
      : vi.fn().mockResolvedValue(account),
  };
  return render(AlpacaDeployDrawerComponent, {
    providers: [
      provideRouter([]),
      { provide: BrokersService, useValue: brokers },
      {
        provide: BrokerV2PanelService,
        useValue: {
          getDeployView: vi.fn().mockResolvedValue(view),
          previewStartAdmission: vi.fn(),
          deployBot: vi.fn(),
        },
      },
    ],
    inputs: { visible: true },
  });
}

describe('AlpacaDeployDrawerComponent', () => {
  it('names the paper world in its header on a paper account', async () => {
    await renderDrawer(fakeAccount());

    expect(await screen.findByText('Deploy · PA9 · paper')).toBeTruthy();
  });

  it('names the shadow world in its header on a live account', async () => {
    // ADR 0059 D2: this header is the Shadow form's only title. A literal
    // `paper` here is the false safety signal slice 4 exists to remove.
    await renderDrawer(
      fakeAccount({ account_id: '9LIVE0001', account_mode: 'live' }),
      SHADOW_DEPLOY_VIEW,
    );

    expect(await screen.findByText('Deploy · 9LIVE0001 · shadow')).toBeTruthy();
    expect(screen.queryByText(/· paper/)).toBeNull();
  });

  it('names no world at all while no account read has answered', async () => {
    await renderDrawer(new Error('account read failed'));

    expect(await screen.findByText('Deploy · Alpaca')).toBeTruthy();
    expect(
      await screen.findByText('Restore the Alpaca account connection, then reopen Deploy.'),
    ).toBeTruthy();
  });
});
