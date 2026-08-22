import { provideRouter } from '@angular/router';
import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { DeployLaunchReceiptComponent } from './deploy-launch-receipt.component';
import type { DeployBotReceipt } from '../v2-panel/lib/broker-v2-panel.service';

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
  admission: {
    operation: 'START',
    allowed: true,
    reason_code: 'START_ADMITTED',
    explanation: 'The process slot is absent, market data is ready, and the Clerk proves flat custody.',
    next_step: null,
    strategy_instance_id: 'spy-test-01',
    proposed_run_id: 'run-1',
    configuration_hash: 'a'.repeat(64),
    account_id: 'PA9',
    evaluated_at_ms: 1_700_000_000_000,
    fact_ages_ms: { runtime: 5, process: 10, market_data: 20, market_liveness: 20, clerk: 30, program_build: 15 },
    evidence_refs: ['test-admission'],
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

describe('DeployLaunchReceiptComponent', () => {
  it('omits the Parameters row when nothing diverges from registered defaults', async () => {
    await render(DeployLaunchReceiptComponent, {
      providers: [provideRouter([])],
      inputs: { receipt: RECEIPT },
    });

    expect(screen.queryByText('Parameters')).toBeNull();
  });

  it('renders diverged parameter names through the receiptLabel pipe, not as raw backend identifiers', async () => {
    await render(DeployLaunchReceiptComponent, {
      providers: [provideRouter([])],
      inputs: {
        receipt: { ...RECEIPT, parameters_diverge_from_defaults: ['rsi_min', 'short_window'] },
      },
    });

    expect(screen.getByText(/Rsi Min, Short Window/)).toBeTruthy();
    expect(screen.queryByText(/rsi_min/)).toBeNull();
    expect(screen.queryByText(/short_window/)).toBeNull();
  });
});
