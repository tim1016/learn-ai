import { ActivatedRoute, convertToParamMap, provideRouter } from '@angular/router';
import { render, screen } from '@testing-library/angular';
import { of } from 'rxjs';
import { describe, expect, it, vi } from 'vitest';

import type { BrokerAccountSnapshot } from '../../../api/alpaca.types';
import { AlpacaDeployTabComponent } from './alpaca-deploy-tab.component';
import { AlpacaDeskAccountDataService } from '../alpaca-desk/alpaca-desk-account-data.service';
import { BrokerV2PanelService } from '../../broker/v2-panel/lib/broker-v2-panel.service';
import { BrokersService } from '../../../services/brokers.service';
import { DEPLOY_VIEW } from '../../broker/broker-deploy-page/alpaca-deploy-workflow.fixtures';
import { provideFleetDirectory, testLane } from '../../../fleet/fleet-directory-testing';
import { resourceTarget } from '../../../fleet/resource-target';

const TARGET = resourceTarget('alpaca', 'clrk_deploy_tab', {
  accountId: 'PA9', bindingGeneration: 3, routingEpoch: 7,
});

function fakeAccount(): BrokerAccountSnapshot {
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
  };
}

function activatedRoute(clerkId: string) {
  const initial = convertToParamMap({ clerkId });
  const noQuery = convertToParamMap({});
  return {
    provide: ActivatedRoute,
    // `AlpacaDeployWorkflowComponent` mounts as a child of this component and
    // reads its own `?strategy=` off the same injected `ActivatedRoute`, so
    // this fake has to answer `queryParamMap` too, not just `paramMap`.
    useValue: {
      paramMap: of(initial),
      snapshot: { paramMap: initial, queryParamMap: noQuery },
      queryParamMap: of(noQuery),
    },
  };
}

interface AccountDataDouble {
  readonly hasAccount: boolean;
}

function accountData({ hasAccount }: AccountDataDouble) {
  return {
    provide: AlpacaDeskAccountDataService,
    useValue: {
      target: () => TARGET,
      accountId: () => 'PA9',
      fence: () => ({ bindingGeneration: 3, routingEpoch: 7 }),
      account: { hasValue: () => hasAccount, value: () => fakeAccount() },
    },
  };
}

async function renderDeployTab(options: {
  readonly lane?: Parameters<typeof testLane>[0] | null;
  readonly hasAccount?: boolean;
} = {}) {
  const { lane = {}, hasAccount = true } = options;
  return render(AlpacaDeployTabComponent, {
    providers: [
      provideRouter([]),
      activatedRoute('clrk_deploy_tab'),
      accountData({ hasAccount }),
      lane === null
        ? provideFleetDirectory({ observed_at_ms: 1_757_000_000_000, clerks: [] })
        : provideFleetDirectory({ observed_at_ms: 1_757_000_000_000, clerks: [testLane({ clerk_id: 'clrk_deploy_tab', ...lane })] }),
      { provide: BrokersService, useValue: { getAccount: vi.fn().mockResolvedValue(fakeAccount()) } },
      {
        provide: BrokerV2PanelService,
        useValue: {
          getDeployView: vi.fn().mockResolvedValue(DEPLOY_VIEW),
          getCatalog: vi.fn().mockResolvedValue([]),
          previewStartAdmission: vi.fn(),
          deployBot: vi.fn(),
        },
      },
    ],
  });
}

describe('AlpacaDeployTabComponent', () => {
  it('hosts the deploy workflow inline when the lane declares deploy capability and the account is confirmed', async () => {
    await renderDeployTab();

    expect(await screen.findByRole('heading', { name: 'Bot binding' })).toBeTruthy();
  });

  it('explains in place when this clerk has no resolved lane', async () => {
    await renderDeployTab({ lane: null });

    expect(await screen.findByText('Deploy is unavailable.')).toBeTruthy();
    expect(
      screen.getByText('This account’s lane has not resolved, so Deploy has no clerk to target.'),
    ).toBeTruthy();
  });

  it('explains in place when the lane does not declare deploy capability', async () => {
    await renderDeployTab({ lane: { capabilities: ['account_read', 'bot_panel_read'] } });

    expect(await screen.findByText('Deploy is unavailable.')).toBeTruthy();
    expect(screen.getByText('This clerk does not declare Deploy capability.')).toBeTruthy();
  });

  it('explains in place when Alpaca has not confirmed the account yet', async () => {
    await renderDeployTab({ hasAccount: false });

    expect(await screen.findByText('Deploy is unavailable.')).toBeTruthy();
    expect(screen.getByText('Alpaca has not confirmed this account yet.')).toBeTruthy();
  });
});
