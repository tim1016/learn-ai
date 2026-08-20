import { fireEvent, render, screen } from '@testing-library/angular';
import { provideRouter } from '@angular/router';
import { describe, expect, it } from 'vitest';

import type { BotPanelView } from '../../lib/broker-v2-panel.types';
import { OperatorBotBannerComponent } from './operator-bot-banner.component';

const PANEL: BotPanelView = {
  strategy_instance_id: 'ema-spy-001', strategy_key: 'ema_crossover', strategy_label: 'EMA crossover',
  broker: 'alpaca', account_id: 'acc-1', symbol: 'SPY', mode: 'trade', updated_at_ms: 1_753_800_000_000, revision: 1,
  market_pulse: {
    session: 'OPEN', market_state: 'TRADABLE', market_liveness_reason: 'Fresh test evidence proves tradability.', market_liveness_observed_at_ms: 1_753_800_001_000, halted_symbol: null, feed_state: 'LIVE', latest_bar_at_ms: 1_753_800_000_000,
    age_ms: 1_000, source: 'polygon', expected_cadence_ms: 60_000,
    headline: 'Market data live', explanation: 'Current.', next_step: null,
    attention_required: false, observed_at_ms: 1_753_800_001_000,
  },
  mission_verdict: {
    state: 'working', label: 'Mission working', explanation: 'Current.',
    next_action: 'Monitor.', evaluated_at_ms: 1_753_800_001_000,
  },
  execution_policy: 'Paper orders only.',
  health: {
    strategy_instance_id: 'ema-spy-001', phase: 'ON_DUTY', phase_label: 'On duty',
    desired_state: 'RUNNING', desired_state_label: 'Running', running: true,
    duty_outcome: null, last_decision_at_ms: null, decision_stale: false,
    last_bar_at_ms: null, resume_eligible: false, resume_label: 'Running',
    resume_explanation: 'Current.', carryover_checkpoint_exposure: {},
  },
  clerk: {
    account_id: 'acc-1', hold_active: false, hold_reason: 'NO_HOLD', hold_reason_label: 'No hold',
    hold_reason_explanation: 'Current.', hold_since_ms: null, freeze_active: false,
    freeze_category: null, freeze_label: 'No freeze', freeze_explanation: 'Current.',
    freeze_next_step: null, freeze_observed_at_ms: null, reconciliation_verdict: null,
    reconciliation_verdict_label: null, last_sweep_at_ms: null, outstanding_intents: 0, channels: [],
  },
  rail: { transaction_ref: null, stations: [] }, journal_tail_ref: '', journal_tail_seq: null,
  actions: [
    { action_id: 'stop', label: 'Stop', explanation: 'Stop bot.', enabled: true, blockers: [], confirmation: null, revision: 1, concurrency_token: 'stop-token' },
    { action_id: 'retire', label: 'Retire & replace', explanation: 'Retire bot.', enabled: true, blockers: [], confirmation: null, revision: 1, concurrency_token: 'retire-token' },
    { action_id: 'rebuild_from_mirror', label: 'Rebuild from mirror', explanation: 'Recover custody.', enabled: true, blockers: [], confirmation: null, revision: 1, concurrency_token: 'rebuild-token' },
  ],
  primary_action_by_lens: { trader: 'stop', operator: 'stop' },
  readiness_checks: [], readiness_ready_count: 0, readiness_blocked_count: 0,
  exposure: {}, working_orders: [], recent_decisions: [], recent_fills: [], fills_today: 0,
  realized_pnl_today: 0, open_pnl: null,
};

describe('OperatorBotBannerComponent', () => {
  it('combines operator market context with direct lifecycle and recovery actions', async () => {
    await render(OperatorBotBannerComponent, {
      inputs: {
        panel: PANEL,
        tickerQuote: { ticker: 'SPY', price: 512.3, changePercent: 0.6 },
      },
      providers: [provideRouter([])],
    });

    expect(screen.getByRole('link', { name: /alpaca bots/i })).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'EMA crossover', level: 1 })).toBeTruthy();
    expect(screen.getByText('ema-spy-001')).toBeTruthy();
    expect(screen.getByText('SPY')).toBeTruthy();
    expect(screen.getByRole('status', { name: 'Mission working' })).toBeTruthy();
    expect(screen.getByRole('status', { name: 'Revision 1 running' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Stop' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'More operator actions' })).toBeTruthy();
    expect(screen.queryByText('Paper')).toBeNull();
    expect(screen.queryByText('acc-1')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'More operator actions' }));
    expect(screen.getByRole('button', { name: 'Retire & replace' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Rebuild from mirror' })).toBeTruthy();
  });

  it('renders the backend-selected recovery-primary action instead of the routine lifecycle command (#1665)', async () => {
    const panel: BotPanelView = {
      ...PANEL,
      primary_action_by_lens: { trader: null, operator: 'rebuild_from_mirror' },
    };

    await render(OperatorBotBannerComponent, {
      inputs: { panel, tickerQuote: null },
      providers: [provideRouter([])],
    });

    expect(screen.getByRole('button', { name: 'Rebuild from mirror' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Stop' })).toBeNull();
  });

  it('renders no primary banner action when the backend reference is null (#1665)', async () => {
    const panel: BotPanelView = {
      ...PANEL,
      primary_action_by_lens: { trader: null, operator: null },
    };

    await render(OperatorBotBannerComponent, {
      inputs: { panel, tickerQuote: null },
      providers: [provideRouter([])],
    });

    expect(screen.queryByRole('button', { name: 'Stop' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Rebuild from mirror' })).toBeNull();
  });
});
