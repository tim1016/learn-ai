import { HttpErrorResponse } from '@angular/common/http';
import { fireEvent, render, screen } from '@testing-library/angular';
import { provideRouter } from '@angular/router';
import { describe, expect, it, vi } from 'vitest';

import {
  BrokerV2PanelService,
  type DeployBotBody,
  type DeployBotReceipt,
  type DeployBotView,
} from '../v2-panel/lib/broker-v2-panel.service';
import { AlpacaDeployWorkflowComponent } from './alpaca-deploy-workflow.component';

const VALIDATION_STRATEGY: DeployBotView['strategies'][number] = {
  strategy_key: 'deployment_validation',
  label: 'Deployment Validation',
  explanation: 'Validated canonical decision kernel.',
  validation_case_symbol: 'SPY',
};

const EMA_STRATEGY: DeployBotView['strategies'][number] = {
  strategy_key: 'ema_crossover_signal',
  label: 'EMA Crossover Signal',
  explanation: 'Validated EMA(5), EMA(10), and RSI(14) crossover signal.',
  validation_case_symbol: 'SPY',
};

const DEPLOY_VIEW: DeployBotView = {
  broker: 'alpaca',
  account_id: 'PA9',
  account_mode: 'paper',
  account_label: 'Alpaca paper · PA9',
  evaluated_at_ms: 1_700_000_000_000,
  eligibility: {
    eligible: true,
    reason_code: 'ALPACA_PAPER_DEPLOY_READY',
    headline: 'This Alpaca paper account is eligible.',
    explanation: 'Every ENTER and EXIT is executed through the Alpaca Clerk.',
    next_action: 'Review the ticket and deploy the paper bot.',
  },
  strategies: [VALIDATION_STRATEGY, EMA_STRATEGY],
  execution_modes: [
    { mode: 'paper', label: 'Paper', availability: 'available', explanation: 'Available through the Alpaca Clerk.' },
    { mode: 'live', label: 'Live', availability: 'planned', explanation: 'Live Alpaca execution is planned.' },
  ],
  readiness_checks: [
    {
      gate_id: 'strategy.validation',
      label: 'Strategy validation',
      ready: true,
      scope: 'strategy',
      authority: 'Strategy validation manifest',
      headline: 'Current accepted evidence is present.',
      explanation: 'The selected strategy has a current human validation receipt.',
      evidence_summary: 'Accepted at the current manifest revision.',
      evidence: { verdict: 'accepted_for_deploy' },
      recovery: null,
    },
    {
      gate_id: 'broker.channel',
      label: 'Broker channel',
      ready: true,
      scope: 'broker',
      authority: 'Alpaca channel health',
      headline: 'Trading and account channels are healthy.',
      explanation: 'The current channel observation admits deployment.',
      evidence_summary: 'Account and trading channels are healthy.',
      evidence: { trading: true, account: true },
      recovery: null,
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
  carryover_available: false,
  carryover_label: 'Allow Clerk-proven exposure carryover on STOP',
  carryover_explanation: 'Account policy currently forbids carried exposure.',
  allowed_actions: ['deploy'],
};

const RECEIPT: DeployBotReceipt = {
  outcome: 'success',
  status: 'deployed',
  receipt_id: 'alpaca-paper-deploy:PA9:spy-test-01:1700000000000',
  recorded_at_ms: 1_700_000_000_000,
  account_id: 'PA9',
  execution_mode: 'paper',
  sizing: { preset: 'safe_canary', quantity: 1 },
  carryover_policy: 'FORBID',
  message: 'spy-test-01 is on duty in Alpaca paper.',
  explanation: 'The deployment binding is durable and Clerk governed.',
  next_action: 'Open the production bot control page.',
  panel_path: '/brokers/alpaca/accounts/PA9/bots/spy-test-01',
  action_plan: {
    on_enter: [{
      leg_id: 'primary',
      instrument: { kind: 'stock', underlying: 'SPY' },
      position: 'long',
      qty_ratio: 1,
    }],
    on_exit: [{ kind: 'close_leg', entry_leg_id: 'primary' }],
  },
  bot: {
    strategy_instance_id: 'spy-test-01',
    broker: 'alpaca',
    symbol: 'SPY',
    mode: 'trade',
    quantity: 1,
    carryover_policy: 'FORBID',
    carryover_checkpoint_exposure: {},
    carryover_checkpoint_config_matches: false,
    running: true,
    phase: 'ON_DUTY',
    desired_state: 'RUNNING',
    active_run_id: 'run-1',
    duty_outcome: null,
    binding_created_at_ms: 1_700_000_000_000,
    last_transition_at_ms: 1_700_000_000_001,
  },
};

function mockService(
  result: DeployBotReceipt | HttpErrorResponse = RECEIPT,
  view: DeployBotView = DEPLOY_VIEW,
) {
  return {
    getDeployView: vi.fn().mockResolvedValue(view),
    deployBot: result instanceof HttpErrorResponse
      ? vi.fn().mockRejectedValue(result)
      : vi.fn().mockResolvedValue(result),
  };
}

async function renderWorkflow(service = mockService()) {
  const rendered = await render(AlpacaDeployWorkflowComponent, {
    providers: [
      provideRouter([]),
      { provide: BrokerV2PanelService, useValue: service },
    ],
    componentInputs: { accountId: 'PA9' },
  });
  await screen.findByText(DEPLOY_VIEW.eligibility.headline);
  return rendered;
}

describe('AlpacaDeployWorkflowComponent', () => {
  it('defaults to a concise trader view without duplicating strategy provenance', async () => {
    await renderWorkflow();

    expect(screen.getAllByText('Deployment Validation').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText('✓ Validated')).toBeTruthy();
    expect(screen.getByText('Live Alpaca execution is planned.')).toBeTruthy();
    expect(screen.queryByText('Strategy provenance')).toBeNull();
    expect(screen.queryByText('Every deployment gate')).toBeNull();
    expect(screen.getAllByText('One long stock ENTER and one matching close-leg EXIT.').length)
      .toBeGreaterThanOrEqual(1);
  });

  it('reveals every admission gate in the operator lens', async () => {
    await renderWorkflow();

    fireEvent.click(screen.getByRole('tab', { name: 'Operator' }));

    expect(screen.getByText('Every deployment gate')).toBeTruthy();
    expect(screen.getByText('Current accepted evidence is present.')).toBeTruthy();
    expect(screen.getByText('Accepted at the current manifest revision.')).toBeTruthy();
  });

  it('changes the validated strategy and submits the selected strategy key', async () => {
    const service = mockService();
    await renderWorkflow(service);

    fireEvent.input(screen.getByLabelText('Bot name'), {
      target: { value: 'ema-paper-01' },
    });
    fireEvent.change(screen.getByLabelText('Validated strategy'), {
      target: { value: 'ema_crossover_signal' },
    });

    expect(screen.getAllByText('EMA Crossover Signal').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText(EMA_STRATEGY.explanation)).toBeTruthy();
    expect(screen.getByRole('link', { name: 'View validation' }).getAttribute('href'))
      .toBe('/strategy-validation?strategy=ema_crossover_signal');

    fireEvent.click(screen.getByRole('button', { name: 'Deploy paper bot' }));

    expect((service.deployBot.mock.calls[0][2] as DeployBotBody).strategy_key)
      .toBe('ema_crossover_signal');
  });

  it('submits only the closed paper canary command and renders its durable receipt', async () => {
    const service = mockService();
    const { fixture } = await renderWorkflow(service);
    const component = fixture.componentInstance as AlpacaDeployWorkflowComponent;

    component['ticket'].update((ticket) => ({ ...ticket, instanceId: 'spy-test-01' }));
    await component['submit']();
    fixture.detectChanges();

    const body = service.deployBot.mock.calls[0][2] as DeployBotBody;
    expect(body).toEqual({
      strategy_instance_id: 'spy-test-01',
      strategy_key: 'deployment_validation',
      symbol: 'SPY',
      sizing: { preset: 'safe_canary', quantity: 1 },
      carryover_policy: 'FORBID',
    });
    expect(body).not.toHaveProperty('mode');
    expect(screen.getByText(RECEIPT.receipt_id)).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Open bot control' }).getAttribute('href'))
      .toBe(RECEIPT.panel_path);
  });

  it('submits bounded custom whole-share sizing', async () => {
    const service = mockService();
    const { fixture } = await renderWorkflow(service);
    const component = fixture.componentInstance as AlpacaDeployWorkflowComponent;

    component['ticket'].update((ticket) => ({
      ...ticket,
      instanceId: 'spy-test-02',
      sizingPreset: 'custom',
      quantity: 7,
    }));
    await component['submit']();

    expect((service.deployBot.mock.calls[0][2] as DeployBotBody).sizing)
      .toEqual({ preset: 'custom', quantity: 7 });
  });

  it('exposes available Clerk-proven carryover to the trader and submits the opt-in', async () => {
    const carryoverView: DeployBotView = {
      ...DEPLOY_VIEW,
      carryover_available: true,
      carryover_explanation: 'Keep exactly attributed exposure only after a durable Clerk checkpoint.',
    };
    const service = mockService(RECEIPT, carryoverView);
    const { fixture } = await renderWorkflow(service);
    const component = fixture.componentInstance as AlpacaDeployWorkflowComponent;

    fireEvent.click(screen.getByRole('checkbox', {
      name: /Allow Clerk-proven exposure carryover on STOP/i,
    }));
    component['ticket'].update((ticket) => ({ ...ticket, instanceId: 'spy-carryover' }));
    await component['submit']();

    expect((service.deployBot.mock.calls[0][2] as DeployBotBody).carryover_policy)
      .toBe('ALLOW');
  });

  it('normalizes text fields before Signal Form validation and submission gating', async () => {
    const { fixture } = await renderWorkflow();
    const component = fixture.componentInstance as AlpacaDeployWorkflowComponent;

    fireEvent.input(screen.getByPlaceholderText('alpaca-spy-01'), {
      target: { value: ' spy-validation-01 ' },
    });
    fireEvent.input(screen.getByPlaceholderText('SPY'), {
      target: { value: ' brk.b ' },
    });
    component['ticketForm'].instanceId().markAsTouched();
    component['ticketForm'].symbol().markAsTouched();
    fixture.detectChanges();

    expect(component['ticket']().instanceId).toBe('spy-validation-01');
    expect(component['ticket']().symbol).toBe('BRK.B');
    expect(component['ticketForm']().valid()).toBe(true);
    expect(screen.getByRole('button', { name: 'Deploy paper bot' }).hasAttribute('disabled'))
      .toBe(false);
  });

  it('distinguishes a stale-state conflict from an unknown outcome', async () => {
    const error = new HttpErrorResponse({
      status: 409,
      error: {
        detail: {
          outcome: 'conflict',
          receipt_id: 'deploy-conflict-1',
          recorded_at_ms: 1_700_000_000_002,
          message: 'Deployment readiness changed.',
          why: 'The Clerk entered a hold after the page loaded.',
          next_action: 'Reload readiness and resolve the hold.',
        },
      },
    });
    const { fixture } = await renderWorkflow(mockService(error));
    const component = fixture.componentInstance as AlpacaDeployWorkflowComponent;
    component['ticket'].update((ticket) => ({ ...ticket, instanceId: 'spy-conflict' }));

    await component['submit']();
    fixture.detectChanges();

    expect(screen.getByText('State changed before launch')).toBeTruthy();
    expect(screen.getByText('The Clerk entered a hold after the page loaded.')).toBeTruthy();
    expect(screen.getByText('deploy-conflict-1')).toBeTruthy();
  });
});
