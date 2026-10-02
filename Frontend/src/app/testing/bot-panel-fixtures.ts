/**
 * Shared `BotPanelView` fixture for surfaces that render a single bot's panel.
 *
 * Defaults describe a healthy, on-duty bot with no blockers; each spec
 * overrides only the fields its assertion is about.
 */

import type {
  BotCatalogView,
  BotPageView,
  BotPanelView,
  ChartFeedView,
  PanelAction,
  ToolbarActionView,
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
  const panel: BotPanelView = {
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
  return 'bot_page' in overrides ? panel : { ...panel, bot_page: fakeBotPage(panel) };
}

interface FakeToolbarEntry {
  readonly label: string;
  readonly group: ToolbarActionView['group'];
  readonly tone: ToolbarActionView['tone'];
  readonly notNeeded: string;
}

/** The custody actions in the backend's toolbar order and plain names (`bot_page_projection`). */
const CUSTODY_ENTRIES: Readonly<Partial<Record<ToolbarActionView['action_id'], FakeToolbarEntry>>> = {
  stop_bot_decisions: { label: 'Stop', group: 'bot', tone: 'danger', notNeeded: 'This bot is not running.' },
  prepare_safe_flatten: { label: 'Sell', group: 'bot', tone: 'danger', notNeeded: 'This bot holds no shares.' },
  reconcile_now: { label: 'Check against Alpaca', group: 'fix', tone: 'neutral', notNeeded: 'Nothing to check.' },
  cancel_verified_working_orders: {
    label: 'Cancel open orders', group: 'fix', tone: 'danger', notNeeded: 'This bot has no open orders.',
  },
  discharge_attributed_residue: {
    label: 'Write off missing shares', group: 'fix', tone: 'danger', notNeeded: 'No shares are missing.',
  },
  recover_exact_execution_evidence: {
    label: 'Recover a missing fill', group: 'fix', tone: 'warning', notNeeded: 'No fill is missing.',
  },
  resolve_execution_coverage: {
    label: 'Use the exact fill', group: 'fix', tone: 'warning', notNeeded: 'No fill is recorded two ways.',
  },
  open_custody_timeline: { label: 'Custody timeline', group: 'inspect', tone: 'neutral', notNeeded: 'Nothing to show.' },
};

function entry(
  action_id: ToolbarActionView['action_id'],
  label: string,
  group: ToolbarActionView['group'],
  availability: ToolbarActionView['availability'],
  reason: string,
  tone: ToolbarActionView['tone'] = 'neutral',
): ToolbarActionView {
  return { action_id, label, group, availability, reason, tone, primary: false };
}

/**
 * What the backend leads a bot page with (`bot_page_projection.bot_page_view`),
 * derived from the fixture's own actions, health and exposure so a spec that
 * overrides those sees the toolbar the backend would send.
 */
export function fakeBotPage(panel: BotPanelView): BotPageView {
  const running = panel.health.running;
  const held = Object.entries(panel.exposure);
  const status: BotPageView['status'] = running
    ? { state: 'running', label: 'Running', reason: null }
    : held.length > 0
      ? { state: 'ended_holding', label: 'Ended holding', reason: null }
      : { state: 'finished', label: 'Finished', reason: null };
  const custody = (actionId: ToolbarActionView['action_id']): ToolbarActionView[] => {
    const action = panel.actions.find((candidate) => candidate.action_id === actionId);
    const spec = CUSTODY_ENTRIES[actionId];
    if (action === undefined || spec === undefined) return [];
    const label = actionId === 'prepare_safe_flatten' && held.length === 1
      ? `Sell ${held[0][1]} ${held[0][0]}`
      : spec.label;
    const needed = (action.needed ?? true) || (actionId === 'stop_bot_decisions' && running);
    if (action.enabled) return [entry(actionId, label, spec.group, 'available', action.explanation, spec.tone)];
    if (!needed) return [entry(actionId, label, spec.group, 'not_needed', spec.notNeeded, spec.tone)];
    return [entry(actionId, label, spec.group, 'blocked', action.blockers[0]?.headline ?? action.explanation, spec.tone)];
  };
  const archive = panel.actions.find((action) => action.action_id === 'archive');
  const toolbar: ToolbarActionView[] = [
    ...custody('stop_bot_decisions'),
    ...custody('prepare_safe_flatten'),
    panel.end?.editable
      ? entry('change_end', 'Change end', 'bot', 'available', 'Change when this bot ends and what it does then.')
      : entry('change_end', 'Change end', 'bot', 'not_needed', 'This bot has no end to change.'),
    running
      ? entry('deploy_again', 'Deploy again', 'bot', 'not_needed', 'This bot is still running.')
      : entry('deploy_again', 'Deploy again', 'bot', 'available', 'Deploy a new bot with these settings.'),
    archive === undefined
      ? entry('archive', 'Clear from Home', 'bot', 'not_needed', 'A running bot stays on Home.')
      : entry('archive', 'Clear from Home', 'bot', archive.enabled ? 'available' : 'blocked',
        archive.enabled ? 'Take this finished bot off Home.' : archive.blockers[0]?.headline ?? archive.explanation),
    panel.mode === 'dry_run' || panel.status === 'cleared'
      ? entry('manual_order', 'Manual order', 'bot', 'not_needed', 'This bot places no manual orders.')
      : entry('manual_order', 'Manual order', 'bot', 'available', `Place an order for ${panel.symbol} on this account.`),
    ...custody('reconcile_now'),
    ...custody('cancel_verified_working_orders'),
    ...custody('discharge_attributed_residue'),
    ...custody('recover_exact_execution_evidence'),
    ...custody('resolve_execution_coverage'),
    ...custody('open_custody_timeline'),
    panel.program_build.state === 'NOT_APPLICABLE'
      ? entry('build_proof', 'Build proof', 'inspect', 'not_needed', 'This strategy has no sealed program to prove.')
      : entry('build_proof', 'Build proof', 'inspect', 'available', 'Show this run\'s build proof and its hashes.'),
  ];
  const primaryId = panel.primary_action === 'execute_safe_flatten' ? 'prepare_safe_flatten' : panel.primary_action;
  const primary = primaryId ?? (status.state === 'finished' ? 'deploy_again' : null);
  return {
    status,
    summary: {
      text: running ? 'Running since Tue Nov 14 2023, 17:13 ET · no decisions, no trades.'
        : 'Ran Tue Nov 14 2023, 17:13–17:14 ET · stopped · no decisions, no trades.',
      template_version: 1,
      facts: {
        run_id: 'run-fixture',
        started_at_ms: OBSERVED_AT_MS - 60_000,
        ended_at_ms: running ? null : OBSERVED_AT_MS,
        scheduled_end_at_ms: null,
        ending: running ? 'running' : 'stopped',
        decision_count: 0,
        decision_count_is_floor: false,
        trade_count: 0,
        set_aside_usd: null,
        returned_usd: null,
        held: held.map(([symbol, quantity]) => ({ symbol, quantity: String(quantity) })),
        exit_queued: false,
        authored_at_ms: OBSERVED_AT_MS,
      },
    },
    toolbar: toolbar.map((item) =>
      item.action_id === primary && item.availability === 'available' ? { ...item, primary: true } : item),
    health: {
      run: [{
        key: 'feed', label: 'IBKR market data feed', state: 'ok', value: 'Continuous',
        note: 'No IBKR market-data interruptions are recorded in this run.', at_ms: null,
      }],
      account: [{ key: 'holds', label: 'Holds', state: 'ok', value: 'None', note: null, at_ms: null }],
    },
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
  { needed = false }: { needed?: boolean } = {},
): PanelAction {
  // The default bot holds nothing, so the policy says each of these has nothing to do (`PanelAction.needed`).
  return fakePanelAction(actionId, { label, enabled: false, blockers: sqliteBlockers(reason), needed });
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
        needed: false,
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
