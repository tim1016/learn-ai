import { HttpErrorResponse } from '@angular/common/http';
import { render, screen } from '@testing-library/angular';
import { provideRouter } from '@angular/router';
import { describe, expect, it, vi } from 'vitest';

import {
  BrokerV2PanelService,
  type DeployBotBody,
  type DeployBotReceipt,
  type DeployBotView,
} from '../lib/broker-v2-panel.service';
import { DeployDialogComponent } from './deploy-dialog.component';

const DEPLOY_VIEW: DeployBotView = {
  broker: 'alpaca',
  account_id: 'PA9',
  account_mode: 'paper',
  account_label: 'Alpaca paper · PA9',
  eligibility: {
    eligible: true,
    reason_code: 'ALPACA_PAPER_DEPLOY_READY',
    headline: 'This Alpaca paper account is eligible.',
    explanation: 'Every ENTER and EXIT is executed through the Alpaca Clerk.',
    next_action: 'Choose the symbol and sizing, then deploy the bot.',
  },
  strategies: [
    {
      strategy_key: 'deployment_validation',
      label: 'Deployment Validation',
      explanation: 'Validated canonical decision kernel.',
    },
  ],
  sizing_options: [
    {
      preset: 'safe_canary',
      label: 'Safe canary · 1 share',
      explanation: 'Fixed one-share sizing.',
      min_quantity: 1,
      max_quantity: 1,
      default_quantity: 1,
    },
    {
      preset: 'custom',
      label: 'Bounded custom shares',
      explanation: 'Whole shares from 1 through 100.',
      min_quantity: 1,
      max_quantity: 100,
      default_quantity: 1,
    },
  ],
  action_plan_explanation: 'One long stock ENTER and one matching close-leg EXIT.',
  allowed_actions: ['deploy'],
};

const RECEIPT: DeployBotReceipt = {
  status: 'deployed',
  message: 'spy-test-01 is on duty in Alpaca paper.',
  explanation: 'The deployment binding is durable and Clerk governed.',
  next_action: 'Open the production bot panel.',
  panel_path: '/brokers/alpaca/accounts/PA9/bots/spy-test-01',
  action_plan: {
    on_enter: [
      {
        leg_id: 'primary',
        instrument: { kind: 'stock', underlying: 'SPY' },
        position: 'long',
        qty_ratio: 1,
      },
    ],
    on_exit: [{ kind: 'close_leg', entry_leg_id: 'primary' }],
  },
  bot: {
    strategy_instance_id: 'spy-test-01',
    broker: 'alpaca',
    symbol: 'SPY',
    mode: 'trade',
    quantity: 1,
    running: true,
    phase: 'ON_DUTY',
    desired_state: 'RUNNING',
    active_run_id: 'run-1',
    duty_outcome: null,
    binding_created_at_ms: 1_700_000_000_000,
    last_transition_at_ms: 1_700_000_000_001,
  },
};

function makeMockPanelService(
  deployResult: DeployBotReceipt | HttpErrorResponse = RECEIPT,
) {
  return {
    getDeployView: vi.fn().mockResolvedValue(DEPLOY_VIEW),
    deployBot: deployResult instanceof HttpErrorResponse
      ? vi.fn().mockRejectedValue(deployResult)
      : vi.fn().mockResolvedValue(deployResult),
  };
}

async function renderDialog(mockPanelService = makeMockPanelService()) {
  const result = await render(DeployDialogComponent, {
    providers: [
      provideRouter([]),
      { provide: BrokerV2PanelService, useValue: mockPanelService },
    ],
    componentInputs: {
      broker: 'alpaca',
      accountId: 'PA9',
      visible: true,
    },
  });
  await screen.findByText(DEPLOY_VIEW.eligibility.headline);
  return result;
}

describe('DeployDialogComponent', () => {
  it('renders only the backend-authored paper workflow', async () => {
    await renderDialog();

    expect(screen.getByText('Alpaca paper · PA9')).toBeTruthy();
    expect(screen.getByText('Deployment Validation')).toBeTruthy();
    expect(screen.getByText(DEPLOY_VIEW.action_plan_explanation)).toBeTruthy();
    expect(screen.queryByText(/log only/i)).toBeNull();
    expect(screen.queryByText(/read.only/i)).toBeNull();
  });

  it('submits safe-canary sizing without client-authored execution mode', async () => {
    const mockPanelService = makeMockPanelService();
    const { fixture } = await renderDialog(mockPanelService);
    const component = fixture.componentInstance as DeployDialogComponent;

    component['form'].controls.strategy_instance_id.setValue('spy-test-01');
    component['form'].controls.symbol.setValue('spy');
    await component['submit']();

    const body = mockPanelService.deployBot.mock.calls[0][2] as DeployBotBody;
    expect(body).toEqual({
      strategy_instance_id: 'spy-test-01',
      strategy_key: 'deployment_validation',
      symbol: 'SPY',
      sizing: { preset: 'safe_canary', quantity: 1 },
    });
    expect(body).not.toHaveProperty('mode');
  });

  it('submits bounded custom sizing selected from the authored contract', async () => {
    const mockPanelService = makeMockPanelService();
    const { fixture } = await renderDialog(mockPanelService);
    const component = fixture.componentInstance as DeployDialogComponent;

    component['form'].controls.strategy_instance_id.setValue('spy-test-02');
    component['form'].controls.symbol.setValue('SPY');
    component['form'].controls.sizing_preset.setValue('custom');
    component['form'].controls.quantity.setValue(7);
    await component['submit']();

    const body = mockPanelService.deployBot.mock.calls[0][2] as DeployBotBody;
    expect(body.sizing).toEqual({ preset: 'custom', quantity: 7 });
  });

  it('renders the backend-authored error explanation and next action', async () => {
    const error = new HttpErrorResponse({
      status: 409,
      error: {
        detail: {
          message: 'Deployment is blocked by the Alpaca Clerk.',
          why: 'An unattributed order requires review.',
          next_action: 'Resolve the Clerk hold, then refresh.',
        },
      },
    });
    const { fixture } = await renderDialog(makeMockPanelService(error));
    const component = fixture.componentInstance as DeployDialogComponent;
    component['form'].controls.strategy_instance_id.setValue('spy-test-01');
    component['form'].controls.symbol.setValue('SPY');

    await component['submit']();
    fixture.detectChanges();

    expect(screen.getByText('Deployment is blocked by the Alpaca Clerk.')).toBeTruthy();
    expect(screen.getByText('An unattributed order requires review.')).toBeTruthy();
    expect(screen.getByText('Resolve the Clerk hold, then refresh.')).toBeTruthy();
  });

  it('keeps the terminal backend receipt visible after deployment', async () => {
    const { fixture } = await renderDialog();
    const component = fixture.componentInstance as DeployDialogComponent;
    component['form'].controls.strategy_instance_id.setValue('spy-test-01');
    component['form'].controls.symbol.setValue('SPY');

    await component['submit']();
    fixture.detectChanges();

    expect(screen.getByText(RECEIPT.message)).toBeTruthy();
    expect(screen.getByText(RECEIPT.next_action)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Open bot panel' })).toBeTruthy();
  });
});
