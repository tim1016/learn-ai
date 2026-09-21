import { provideRouter } from '@angular/router';
import { render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type { BrokerAccountSnapshot } from '../../../api/alpaca.types';
import { BrokersService } from '../../../services/brokers.service';
import { BrokerV2PanelService } from '../v2-panel/lib/broker-v2-panel.service';
import { AlpacaDeployDrawerComponent } from './alpaca-deploy-drawer.component';
import { fakePickerWorld } from '../../../shared/symbol-picker/testing/fake-picker-world';
import { DEPLOY_VIEW, SHADOW_DEPLOY_VIEW } from './alpaca-deploy-workflow.fixtures';
import { resourceTarget } from '../../../fleet/resource-target';

const TARGET = resourceTarget('alpaca', 'clrk_drawer', {
  accountId: 'PA9', bindingGeneration: 3, routingEpoch: 7,
});

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
      ...fakePickerWorld().providers,
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
    inputs: { visible: true, target: TARGET },
  });
}

describe('AlpacaDeployDrawerComponent', () => {
  it('names the paper world in its header on a paper account', async () => {
    await renderDrawer(fakeAccount());

    expect(await screen.findByText('Deploy · PA9 · paper')).toBeTruthy();
  });

  it('names no world in its header on a live account until the deploy view says which', async () => {
    // ADR 0059 D2/D11: a live account is shadowed or live-custodied, and the
    // drawer does not know which until the deploy view says — so it names no
    // world rather than a wrong one. A literal `paper` here is the false
    // safety signal slice 4 exists to remove.
    await renderDrawer(
      fakeAccount({ account_id: '9LIVE0001', account_mode: 'live' }),
      SHADOW_DEPLOY_VIEW,
    );

    expect(await screen.findByText('Deploy · 9LIVE0001')).toBeTruthy();
    expect(screen.queryByText(/· paper/)).toBeNull();
  });

  it('names no world at all while no account read has answered', async () => {
    await renderDrawer(new Error('account read failed'));

    expect(await screen.findByText('Deploy · Alpaca')).toBeTruthy();
    expect(
      await screen.findByText('Restore the Alpaca account connection, then reopen Deploy.'),
    ).toBeTruthy();
  });

  /** #2106: the deploy chain's leaf command surfaces (`AlpacaDeployWorkflowComponent`,
   * `DeployPaperAccessComponent`) trust that `target` is frozen once by this
   * drawer at open and never re-derived from a live directory read while the
   * drawer stays open — that trust is this test's subject, not an assumption.
   * `resolvedTarget()` feeds the `account` resource's reactive `params`, so a
   * second `getAccount` call after a live rebind would prove the freeze had
   * failed; a single call proves it held. */
  it('freezes the target at open and does not re-derive it from a later input change', async () => {
    const getAccount = vi.fn().mockResolvedValue(fakeAccount());
    const view = await render(AlpacaDeployDrawerComponent, {
      providers: [
        ...fakePickerWorld().providers,
        provideRouter([]),
        { provide: BrokersService, useValue: { getAccount } },
        {
          provide: BrokerV2PanelService,
          useValue: {
            getDeployView: vi.fn().mockResolvedValue(DEPLOY_VIEW),
            previewStartAdmission: vi.fn(),
            deployBot: vi.fn(),
          },
        },
      ],
      inputs: { visible: true, target: TARGET },
    });
    await view.fixture.whenStable();
    expect(getAccount).toHaveBeenCalledTimes(1);
    expect(getAccount).toHaveBeenCalledWith(expect.objectContaining({ bindingGeneration: 3, routingEpoch: 7 }));

    // The lane rebinds (a directory refresh) while the drawer stays open.
    view.fixture.componentRef.setInput('target', resourceTarget('alpaca', 'clrk_drawer', {
      accountId: 'PA9', bindingGeneration: 99, routingEpoch: 55,
    }));
    view.fixture.detectChanges();
    await view.fixture.whenStable();

    expect(getAccount).toHaveBeenCalledTimes(1);
  });
});
