/**
 * Shared `BotPanelView` fixture for surfaces that render a single bot's panel.
 *
 * Defaults describe a healthy, on-duty bot with no blockers; each spec
 * overrides only the fields its assertion is about.
 */

import type {
  BotCatalogView,
  BotPanelView,
  PanelAction,
} from '../components/broker/v2-panel/lib/broker-v2-panel.types';

const OBSERVED_AT_MS = 1_700_000_001_000;

export function fakeBotPanelView(overrides: Partial<BotPanelView> = {}): BotPanelView {
  return {
    strategy_instance_id: 'spy-momentum-01',
    strategy_key: 'deployment_validation',
    strategy_label: 'Deployment Validation',
    broker: 'alpaca',
    account_id: 'PA9',
    symbol: 'SPY',
    mode: 'trade',
    sealed_program: null,
    program_build: {
      state: 'NOT_APPLICABLE',
      program_key: 'deployment_validation',
      verified_at_ms: OBSERVED_AT_MS,
      explanation: 'No Signal Program build proof supplied.',
    },
    resume_admission: null,
    updated_at_ms: OBSERVED_AT_MS,
    revision: 1,
    market_pulse: {
      session: 'OPEN',
      market_state: 'TRADABLE',
      market_liveness_reason: 'Fresh test evidence proves tradability.',
      market_liveness_observed_at_ms: OBSERVED_AT_MS,
      halted_symbol: null,
      feed_state: 'LIVE',
      latest_bar_at_ms: 1_700_000_000_000,
      age_ms: 1_000,
      source: 'alpaca',
      expected_cadence_ms: 60_000,
      headline: 'Market data live',
      explanation: 'The feed is current.',
      next_step: null,
      attention_required: false,
      observed_at_ms: OBSERVED_AT_MS,
    },
    mission_verdict: {
      state: 'working',
      label: 'Working',
      explanation: 'The runtime is on duty.',
      next_action: 'Monitor decisions.',
      evaluated_at_ms: OBSERVED_AT_MS,
    },
    execution_policy: 'Observation only.',
    health: {
      strategy_instance_id: 'spy-momentum-01',
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
      resume_explanation: 'This strategy instance already has a live run.',
      carryover_checkpoint_exposure: {},
    },
    clerk: {
      account_id: 'PA9',
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
    journal_tail_ref: '/api/brokers/alpaca/accounts/PA9/bots/spy-momentum-01/journal',
    journal_tail_seq: null,
    actions: [],
    primary_action_by_lens: { trader: null, operator: null },
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

export function fakePanelAction(
  actionId: PanelAction['action_id'],
  overrides: Partial<PanelAction> = {},
): PanelAction {
  return {
    action_id: actionId,
    label: actionId === 'resume' ? 'Resume' : 'Stop',
    explanation: `${actionId} this bot.`,
    enabled: true,
    blockers: [],
    confirmation: null,
    revision: 1,
    concurrency_token: `${actionId}-token`,
    ...overrides,
  };
}

/**
 * One roster row. `day_pnl` mirrors the backend authority
 * (`catalog_projection_service.day_pnl`): null-safe `realized + open`, null
 * only when both components are absent. Override it directly to exercise a
 * projection that disagrees with its components.
 */
export function fakeCatalogBot(overrides: Partial<BotCatalogView> = {}): BotCatalogView {
  const realized = overrides.realized_pnl_today ?? ('realized_pnl_today' in overrides ? null : 45.5);
  const open = overrides.open_pnl ?? null;
  return {
    strategy_instance_id: 'spy-momentum-01',
    broker: 'alpaca',
    account_id: 'PA9',
    symbol: 'SPY',
    phase: 'ON_DUTY',
    desired_state: 'RUNNING',
    running: true,
    strategy_key: 'deployment_validation',
    strategy_label: 'Deployment Validation',
    mode: 'trade',
    status_label: 'Working',
    status_explanation: 'Running under Account Clerk custody.',
    exposure: {},
    fills_today: 2,
    realized_pnl_today: realized,
    open_pnl: open,
    day_pnl: realized === null && open === null ? null : (realized ?? 0) + (open ?? 0),
    last_activity_at_ms: 1_700_000_000_000,
    needs_attention: false,
    ...overrides,
  };
}
