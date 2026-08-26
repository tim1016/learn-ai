import { render, screen } from '@testing-library/angular';
import { provideRouter } from '@angular/router';
import { describe, expect, it } from 'vitest';

import type { BotPanelView } from '../../lib/broker-v2-panel.types';
import { TraderBotBannerComponent } from './trader-bot-banner.component';

const PANEL: BotPanelView = {
  strategy_instance_id: 'ema-spy-001',
  strategy_key: 'ema_crossover',
  strategy_label: 'EMA crossover',
  broker: 'alpaca',
  account_id: 'acc-1',
  symbol: 'SPY',
  mode: 'trade',
  sealed_program: null,
  program_build: {
    state: 'NOT_APPLICABLE',
    program_key: 'ema_crossover',
    verified_at_ms: 1_753_800_000_000,
    explanation: 'No Signal Program build proof supplied.',
  },
  resume_admission: null,
  updated_at_ms: 1_753_800_000_000,
  revision: 1,
  market_pulse: {
    session: 'OPEN', market_state: 'TRADABLE', market_liveness_reason: 'Fresh test evidence proves tradability.', market_liveness_observed_at_ms: 1_753_800_001_000, halted_symbol: null, feed_state: 'LIVE', latest_bar_at_ms: 1_753_800_000_000,
    age_ms: 1_000, source: 'polygon', expected_cadence_ms: 60_000,
    headline: 'Market data live', explanation: 'Current.', next_step: null,
    attention_required: false, observed_at_ms: 1_753_800_001_000,
  },
  mission_verdict: {
    state: 'blocked', label: 'Mission blocked', explanation: 'Clerk hold.',
    next_action: 'Resolve the hold.', evaluated_at_ms: 1_753_800_001_000,
  },
  execution_policy: 'Paper orders only.',
  health: {
    strategy_instance_id: 'ema-spy-001', phase: 'OFF_DUTY', phase_label: 'Off duty',
    desired_state: 'STOPPED', desired_state_label: 'Stopped', running: false,
    duty_outcome: null, last_decision_at_ms: null, decision_stale: false,
    last_bar_at_ms: null, resume_eligible: true, resume_label: 'Ready to resume',
    resume_explanation: 'Ready.', carryover_checkpoint_exposure: {},
  },
  clerk: {
    account_id: 'acc-1', hold_active: true, hold_reason: 'UNEXPLAINED_ORDER_HOLD', hold_reason_label: 'Hold',
    hold_reason_explanation: 'Blocked.', hold_since_ms: null, freeze_active: false,
    freeze_category: null, freeze_label: 'No freeze', freeze_explanation: 'Current.',
    freeze_next_step: null, freeze_observed_at_ms: null, reconciliation_verdict: null,
    reconciliation_verdict_label: null, last_sweep_at_ms: null, outstanding_intents: 0, channels: [],
  },
  rail: { transaction_ref: null, stations: [] }, journal_tail_ref: '', journal_tail_seq: null,
  actions: [{
    action_id: 'resume', label: 'Resume', explanation: 'Resume bot.', enabled: true,
    blockers: [], confirmation: null, revision: 1, concurrency_token: 'resume-token',
  }],
  primary_action_by_lens: { trader: 'resume', operator: 'resume' },
  readiness_checks: [], readiness_ready_count: 0, readiness_blocked_count: 1,
  exposure: {}, working_orders: [], recent_decisions: [], recent_fills: [], fills_today: 0,
  realized_pnl_today: 0, open_pnl: null,
};

describe('TraderBotBannerComponent', () => {
  it('shows one mission verdict and the direct trader actions without legacy header noise', async () => {
    await render(TraderBotBannerComponent, {
      inputs: { panel: PANEL },
      providers: [provideRouter([])],
    });

    expect(screen.getByRole('link', { name: /alpaca bots/i })).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'EMA crossover', level: 1 })).toBeTruthy();
    expect(screen.getByText('ema-spy-001')).toBeTruthy();
    expect(screen.getByRole('status', { name: 'Mission blocked' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Resume' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'More trader actions' })).toBeTruthy();
    expect(screen.queryByText('Paper')).toBeNull();
    expect(screen.queryByText('Runtime idle')).toBeNull();
    expect(screen.queryByText('acc-1')).toBeNull();
  });

  it('never renders an Operator-only action as the primary command, even when presented (#1665)', async () => {
    const panel: BotPanelView = {
      ...PANEL,
      actions: [
        ...PANEL.actions,
        {
          action_id: 'resolve_execution_coverage',
          label: 'Resolve execution coverage',
          explanation: 'Recover custody.',
          enabled: true,
          blockers: [],
          confirmation: null,
          revision: 1,
          concurrency_token: 'rebuild-token',
        },
      ],
      // The backend never selects an Operator-only repair for the Trader lens;
      // this fixture proves the banner defers to that reference rather than
      // re-deriving a primary action from `actions`/`health` on its own.
      primary_action_by_lens: { trader: null, operator: 'resolve_execution_coverage' },
    };

    await render(TraderBotBannerComponent, {
      inputs: { panel },
      providers: [provideRouter([])],
    });

    expect(screen.queryByRole('button', { name: 'Resolve execution coverage' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Resume' })).toBeNull();
  });
});
