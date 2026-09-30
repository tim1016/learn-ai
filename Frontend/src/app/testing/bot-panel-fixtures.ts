/**
 * Shared `BotPanelView` fixture for surfaces that render a single bot's panel.
 *
 * Defaults describe a healthy, on-duty bot with no blockers; each spec
 * overrides only the fields its assertion is about.
 */

import type {
  BotCatalogView,
  BotPanelView,
  ChartFeedView,
  PanelAction,
} from '../components/broker/v2-panel/lib/broker-v2-panel.types';
import { operatorBlockerFixture } from './operator-blocker-fixtures';

const OBSERVED_AT_MS = 1_700_000_001_000;

/**
 * The `detail` `_raise_panel_error` sends for a typed panel refusal
 * (`routers/broker_v2_panel.py`), exactly — e.g. a refused bot end (#2607):
 * 400 when the rules refuse it, 409 when the bot's state does, with
 * `reason_code` `BOT_END_REFUSED`. A refusal with no code sends none. No
 * generated type describes it: an `HTTPException`'s detail is not in the
 * OpenAPI contract.
 */
export interface PanelRefusalDetail {
  readonly message: string;
  readonly why: string | null;
  readonly next_action: string | null;
  readonly reason_code?: string;
}

/** A panel route's refusal body as the page receives it: `{detail: {message, why, next_action, reason_code?}}`.
 * Plain data, so the e2e world can answer with it too. */
export function panelRefusalBody(detail: PanelRefusalDetail): { readonly detail: PanelRefusalDetail } {
  return { detail };
}

/** A `ChartLiveResponse.feed` (#2355): quiet (`LIVE`) unless a spec is about the chart line.
 * An override that changes `state` also sets `show_notice` / `attention_required`:
 * the backend owns that policy and the client reads it as given. */
export function fakeChartFeed(overrides: Partial<ChartFeedView> = {}): ChartFeedView {
  return {
    state: 'LIVE',
    headline: 'Chart feed live',
    explanation: "The chart's IBKR bar line is delivering bars within its expected cadence.",
    next_step: null,
    show_notice: false,
    attention_required: false,
    last_bar_at_ms: null,
    last_error: null,
    ...overrides,
  };
}

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
    feed_continuity: {
      provider_label: 'IBKR market data',
      run_id: 'run-fixture',
      state: 'continuous',
      state_label: 'Continuous',
      explanation: 'No IBKR market-data interruptions are recorded in this run.',
      interruption_count: 0,
      recovery_count: 0,
      unresolved_count: 0,
      decision_impact_count: 0,
      last_interruption_at_ms: null,
      last_recovery_at_ms: null,
      latest_bar_at_ms: 1_700_000_000_000,
      events: [],
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
    primary_action: null,
    exit_terms: null,
    status: 'running',
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
    open_pnl_usd: null,
    open_pnl_direction: null,
    ...overrides,
  };
}

export function fakePanelAction(
  actionId: PanelAction['action_id'],
  overrides: Partial<PanelAction> = {},
): PanelAction {
  return {
    action_id: actionId,
    label: actionId,
    explanation: `${actionId} this bot.`,
    enabled: true,
    blockers: [],
    confirmation: null,
    revision: 1,
    concurrency_token: `${actionId}-token`,
    ...overrides,
  };
}

/** Why the Clerk does not allow a recovery action now, as
 * `sqlite_panel_adapter._capability_blocker` authors it. */
function sqliteBlockers(reason: { id: string; headline: string; detail: string }): PanelAction['blockers'] {
  return [operatorBlockerFixture({
    id: reason.id, scope: 'bot', severity: 'blocking', disposition: 'wait',
    headline: reason.headline, detail: reason.detail, primaryMove: null, appliesTo: 'run',
  })];
}

function unavailableSqliteAction(
  actionId: PanelAction['action_id'],
  label: string,
  reason: { id: string; headline: string; detail: string },
): PanelAction {
  return fakePanelAction(actionId, { label, enabled: false, blockers: sqliteBlockers(reason) });
}

/** The stop the SQLite panel presents for a running bot: the Clerk's
 * `stop_bot_decisions` (`recovery_policy`), with its confirmation, which
 * names the bot it stops (#2634). */
export function fakeSqliteStopAction(
  overrides: Partial<PanelAction> = {},
  { sid = 'spy-momentum-01' }: { sid?: string } = {},
): PanelAction {
  return fakePanelAction('stop_bot_decisions', {
    label: 'Stop bot decisions',
    explanation: 'Stop the bot making new decisions. Stopping doesn\'t sell its shares.',
    confirmation: {
      title: `Stop ${sid}?`,
      body: 'Stop the bot making new decisions. Stopping doesn\'t sell its shares.',
      consequence: 'The bot stops making new decisions. A sale already sent can still go through. Cash it isn\'t using goes back to the account. Its scheduled end is cancelled: nothing is sold at the end time.',
      confirm_label: 'Stop bot decisions',
      required_token: '',
    },
    ...overrides,
  });
}

/**
 * A bot's actions as the SQLite panel really presents them
 * (`sqlite_panel_adapter.adapt_sqlite_panel`): a stopped bot's `archive`
 * (Clear) first, then the Clerk's recovery catalog
 * (`recovery_policy._DESCRIPTORS`) in its order, with its labels and
 * blockers — never a generic `stop`. A bot's stop is `stop_bot_decisions`,
 * allowed only while the bot runs.
 */
export function fakeSqliteBotActions(
  { running = true, sid = 'spy-momentum-01' }: { running?: boolean; sid?: string } = {},
): PanelAction[] {
  const noExposure = {
    id: 'NO_ATTRIBUTED_EXPOSURE',
    headline: 'No attributed exposure requires a flatten plan.',
    detail: 'Run Reconcile now and refresh the custody snapshot.',
  };
  // `action_policy.archive_action` for a stopped, flat bot: the one lifecycle
  // action the SQLite adapter presents, and only once the bot stops.
  const archive = fakePanelAction('archive', {
    label: 'Clear',
    explanation: 'Take a finished bot off Home. It must be stopped and flat, with nothing claimed. '
      + 'Its fills, fees and result stay in Activity. There is no undo.',
    confirmation: {
      title: 'Archive this bot?',
      body: `This takes ${sid} on account PA9 off the roster. It is stopped, with no attributed `
        + 'exposure and 0 working orders.',
      consequence: 'The registration can start no further runs and its id is never reused. Its history and '
        + 'receipts are kept. This cannot be undone.',
      confirm_label: 'Archive bot',
      required_token: 'ARCHIVE',
    },
  });
  return [
    ...(running ? [] : [archive]),
    unavailableSqliteAction('recover_exact_execution_evidence', 'Recover exact execution evidence', {
      id: 'NO_EXECUTION_COVERAGE_CONFLICT',
      headline: 'No active execution-coverage conflict requires historical evidence recovery.',
      detail: 'No historical execution recovery is required.',
    }),
    unavailableSqliteAction('resolve_execution_coverage', 'Resolve execution coverage', {
      id: 'NO_EXECUTION_COVERAGE_CONFLICT',
      headline: 'No active execution-coverage conflict has a Clerk-owned resolution path.',
      detail: 'No coverage resolution is required.',
    }),
    fakePanelAction('reconcile_now', {
      label: 'Reconcile now',
      explanation: 'Compare durable Clerk custody with a fresh Alpaca account observation.',
    }),
    unavailableSqliteAction('cancel_verified_working_orders', 'Cancel verified working orders', {
      id: 'NO_VERIFIED_WORKING_ORDERS',
      headline: 'No working order has both a durable Clerk reference and broker identity.',
      detail: 'Run Reconcile now to refresh exact working-order identity.',
    }),
    unavailableSqliteAction('prepare_safe_flatten', 'Prepare safe flatten', noExposure),
    unavailableSqliteAction('execute_safe_flatten', 'Execute safe flatten', noExposure),
    unavailableSqliteAction('discharge_attributed_residue', 'Discharge stranded residue', {
      id: 'RECOVERY_SCOPE_UNSUPPORTED',
      headline: 'Discharge a residue from the single bot that holds one attributed symbol.',
      detail: 'Open the bot that holds the residue.',
    }),
    running
      ? fakeSqliteStopAction({}, { sid })
      : fakeSqliteStopAction({
        enabled: false,
        blockers: sqliteBlockers({
          id: 'NO_ACTIVE_BOT_RUN',
          headline: 'Select a bot with an active run; no decision process is currently stoppable.',
          detail: 'Choose an active bot or keep the current stopped state.',
        }),
      }, { sid }),
    fakePanelAction('open_custody_timeline', {
      label: 'Open custody timeline',
      explanation: 'Inspect the immutable operation-first evidence timeline.',
    }),
  ];
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
    status_explanation: 'Running · no position',
    exposure: {},
    fills_today: 2,
    realized_pnl_today: realized,
    open_pnl: open,
    day_pnl: realized === null && open === null ? null : (realized ?? 0) + (open ?? 0),
    last_activity_at_ms: 1_700_000_000_000,
    needs_attention: false,
    group: 'running',
    world_label: 'PAPER · practice money',
    ...overrides,
  };
}
