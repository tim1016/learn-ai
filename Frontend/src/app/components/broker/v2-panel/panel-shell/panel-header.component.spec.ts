import { fireEvent, render, screen } from '@testing-library/angular';
import { provideRouter } from '@angular/router';
import { of } from 'rxjs';
import { describe, expect, it, vi } from 'vitest';

import { MarketDataService } from '../../../../services/market-data.service';
import type { BotPanelView, PanelAction } from '../lib/broker-v2-panel.types';
import { PanelHeaderComponent } from './panel-header.component';

function action(actionId: PanelAction['action_id'], label: string): PanelAction {
  return {
    action_id: actionId,
    label,
    explanation: `${label} this bot.`,
    enabled: true,
    blockers: [],
    confirmation: null,
    revision: 1,
    concurrency_token: `${actionId}-token`,
  };
}

function panel(overrides: Partial<BotPanelView> = {}): BotPanelView {
  return {
    strategy_instance_id: 'val-nvda-0804-05',
    strategy_key: 'valuation',
    strategy_label: 'Valuation strategy',
    broker: 'alpaca',
    account_id: 'PA3KWXU1C4C3',
    symbol: 'NVDA',
    mode: 'log_only',
    updated_at_ms: 1_753_800_000_000,
    revision: 1,
    market_pulse: {
      session: 'OPEN',
      feed_state: 'LIVE',
      latest_bar_at_ms: 1_753_800_000_000,
      age_ms: 1_000,
      source: 'polygon',
      expected_cadence_ms: 60_000,
      headline: 'Market data live',
      explanation: 'The latest bar arrived on time.',
      next_step: null,
      attention_required: false,
      observed_at_ms: 1_753_800_001_000,
    },
    mission_verdict: {
      state: 'working',
      label: 'Working',
      explanation: 'The bot is evaluating its mission.',
      next_action: 'Monitor decisions.',
      evaluated_at_ms: 1_753_800_001_000,
    },
    execution_policy: 'Paper orders only.',
    health: {
      strategy_instance_id: 'val-nvda-0804-05',
      phase: 'ON_DUTY',
      phase_label: 'On duty',
      desired_state: 'RUNNING',
      desired_state_label: 'Running',
      running: true,
      duty_outcome: null,
      last_decision_at_ms: null,
      decision_stale: false,
      last_bar_at_ms: null,
      resume_eligible: false,
      resume_label: 'Resume not applicable',
      resume_explanation: 'The bot is running.',
      carryover_checkpoint_exposure: {},
    },
    clerk: {
      account_id: 'PA3KWXU1C4C3',
      hold_active: false,
      hold_reason: 'NO_HOLD',
      hold_reason_label: 'No hold',
      hold_reason_explanation: 'No hold active.',
      hold_since_ms: null,
      freeze_active: false,
      freeze_category: null,
      freeze_label: 'No account freeze',
      freeze_explanation: 'Account truth is current.',
      freeze_next_step: null,
      freeze_observed_at_ms: null,
      reconciliation_verdict: null,
      reconciliation_verdict_label: null,
      last_sweep_at_ms: null,
      outstanding_intents: 0,
      channels: [],
    },
    rail: { transaction_ref: null, stations: [] },
    journal_tail_ref: '',
    journal_tail_seq: null,
    actions: [],
    readiness_checks: [],
    readiness_ready_count: 0,
    readiness_blocked_count: 0,
    exposure: {},
    working_orders: [],
    recent_decisions: [],
    recent_fills: [],
    fills_today: 0,
    realized_pnl_today: 0,
    open_pnl: null,
    ...overrides,
  };
}

function marketData() {
  return {
    getStockSnapshot: vi.fn().mockReturnValue(of({
      success: true,
      snapshot: {
        ticker: 'NVDA',
        day: { open: 178, high: 183, low: 177, close: 181.42, volume: 1, vwap: 180 },
        prevDay: null,
        min: null,
        todaysChange: 2.42,
        todaysChangePercent: 1.35,
        updated: 1_753_800_001_000,
      },
      error: null,
    })),
  };
}

async function renderHeader(value: BotPanelView, actionPending = false) {
  return render(PanelHeaderComponent, {
    inputs: { panel: value, actionPending },
    providers: [
      provideRouter([]),
      { provide: MarketDataService, useValue: marketData() },
    ],
  });
}

describe('PanelHeaderComponent', () => {
  it('renders the canonical ticker and an account-scoped manual-order route', async () => {
    const { container } = await renderHeader(panel());

    expect(await screen.findByTitle(/NVDA —/i)).toBeTruthy();
    expect(container.querySelector('app-asset-identity')?.textContent).toContain('NVDA');
    expect(screen.getByText('$181.42')).toBeTruthy();
    expect(screen.getByText('+1.35%')).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Manual order' }).getAttribute('href')).toBe(
      '/brokers/alpaca?order=new&accountId=PA3KWXU1C4C3&symbol=NVDA',
    );
  });

  it('uses the durable duty outcome as the bot headline', async () => {
    const value = panel();
    await renderHeader(panel({
      health: {
        ...value.health,
        desired_state_label: 'Stopped',
        duty_outcome: {
          kind: 'STOPPED',
          reason_code: 'OPERATOR_STOP',
          label: 'Stopped by operator',
          explanation: 'Stopped safely after the operator command.',
          recorded_at_ms: 1_753_800_001_000,
          run_id: 'run-1',
        },
      },
    }));

    expect(screen.getByRole('status', { name: 'Stopped safely after the operator command.' })).toBeTruthy();
  });

  it.each([
    { running: false, desiredState: 'STOPPED', id: 'resume', label: 'Resume' },
    { running: true, desiredState: 'PAUSED', id: 'continue', label: 'Continue' },
    { running: true, desiredState: 'RUNNING', id: 'stop', label: 'Stop' },
  ] as const)('selects $label from the current lifecycle state', async ({
    running,
    desiredState,
    id,
    label,
  }) => {
    const value = panel();
    await renderHeader(panel({
      health: { ...value.health, running, desired_state: desiredState },
      actions: [action(id, label)],
    }));

    expect(screen.getByRole('button', { name: label })).toBeTruthy();
  });

  it('uses the SQLite stop-decisions action for an active SQLite run', async () => {
    const value = panel();
    await renderHeader(panel({
      health: { ...value.health, running: true, desired_state: 'RUNNING' },
      actions: [action('stop_bot_decisions', 'Stop bot decisions')],
    }));

    const stopButton = screen.getByRole('button', { name: 'Stop bot decisions' });
    expect(stopButton.classList.contains('panel-action__button--danger')).toBe(true);
  });

  it('disables the lifecycle action and exposes pending state while a command is running', async () => {
    const value = panel();
    await renderHeader(panel({
      health: { ...value.health, running: false, desired_state: 'STOPPED' },
      actions: [action('resume', 'Resume')],
    }), true);

    const resume = screen.getByRole('button', { name: 'Resume' });
    expect(resume.hasAttribute('disabled')).toBe(true);
    expect(resume.getAttribute('aria-busy')).toBe('true');
    fireEvent.click(resume);
  });
});
