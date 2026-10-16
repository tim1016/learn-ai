import { fireEvent, render, screen, within } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import axe from 'axe-core';
import { HttpErrorResponse } from '@angular/common/http';
import { NgTemplateOutlet } from '@angular/common';
import { Component, inject } from '@angular/core';
import { TestBed, type ComponentFixture } from '@angular/core/testing';
import { afterAll, afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from 'vitest';
import { MessageService } from 'primeng/api';
import type {
  HistoricalExecutionRecoveryPlan,
  SqliteAttestationFindings,
  SqliteRecoveryAction,
  SqliteRecoveryActionCheck,
  SqliteSafeFlattenPlan,
} from '../../../../api/alpaca.types';
import { BotPanelShellComponent, CURRENT_RUN_POLL_MS } from './bot-panel-shell.component';
import { BrokerV2PanelService, type BotEndView, type DeploymentBudgetView } from '../lib/broker-v2-panel.service';
import { BrokersService, sqliteTimelineQueryFromParams } from '../../../../services/brokers.service';
import { formatTimestampDisplay } from '../../../../shared/timestamp/timestamp-display';
import {
  fakeBotPage,
  fakeChartFeed,
  fakeSqliteSettleAction,
  fakeSqliteStopAction,
} from '../../../../testing/bot-panel-fixtures';
import { DUAL_PANE_CHART_FACTORY } from '../dual-pane-chart/dual-pane-chart.component';
import type {
  BotPanelView,
  BotPanelLiveSnapshot,
  BotRunView,
  PanelAction,
  PanelActionResult,
} from '../lib/broker-v2-panel.types';
import { provideRouter, Router } from '@angular/router';
import {
  provideFleetDirectory,
  testLane,
  type FleetDirectoryDouble,
} from '../../../../fleet/fleet-directory-testing';
import { LANE_FENCE_REFRESH_FAILED_MESSAGE } from '../../../../fleet/lane-fence';
import { fakeStrategyView } from '../../../../testing/strategy-view-fixtures';
import { WorkspaceHeaderSlot } from '../../../brokers/alpaca-workspace/workspace-header-slot.service';

const messageService = { add: vi.fn() };
const chartMocks = vi.hoisted(() => {
  const timeScale = {
    fitContent: vi.fn(),
    subscribeSizeChange: vi.fn(),
    subscribeVisibleLogicalRangeChange: vi.fn(),
    width: vi.fn().mockReturnValue(0),
    timeToCoordinate: vi.fn().mockReturnValue(null),
    options: vi.fn().mockReturnValue({ barSpacing: 6 }),
  };
  const series = { setData: vi.fn(), update: vi.fn(), applyOptions: vi.fn() };
  const chart = {
    addSeries: vi.fn().mockReturnValue(series),
    timeScale: vi.fn().mockReturnValue(timeScale),
    applyOptions: vi.fn(),
    remove: vi.fn(),
  };
  return { chart, createChart: vi.fn().mockReturnValue(chart) };
});

// DualPaneChartComponent -> lightweight-charts: mock for unit tests.
vi.mock('lightweight-charts', () => {
  const createSeriesMarkers = vi.fn().mockReturnValue({ setMarkers: vi.fn() });
  return {
    createChart: chartMocks.createChart,
    createSeriesMarkers,
    CandlestickSeries: 'CandlestickSeries',
    TickMarkType: { Year: 0, Month: 1, DayOfMonth: 2, Time: 3, TimeWithSeconds: 4 },
  };
});

beforeEach(() => {
  TestBed.configureTestingModule({
    providers: [
      // The double's default lane must resolve for this file's routed
      // clerkId ('clrk_spec', not the shared fixture's TEST_CLERK_ID) so the
      // fence tests exercise a real lane rather than a permanently-missing one.
      provideFleetDirectory({
        observed_at_ms: 1_757_000_000_000,
        clerks: [testLane({ clerk_id: 'clrk_spec' })],
      }),
      { provide: DUAL_PANE_CHART_FACTORY, useValue: chartMocks.createChart },
    ],
  });
  chartMocks.createChart.mockClear();
});

const PANEL: BotPanelView = {
  strategy_instance_id: 'sid-001',
  strategy_key: 'ema_crossover',
  strategy_label: 'Ema Crossover',
  broker: 'alpaca',
  account_id: 'DUM284968',
  symbol: 'QQQ',
  mode: 'log_only',
  sealed_program: null,
  program_build: {
    state: 'NOT_APPLICABLE',
    program_key: 'ema_crossover',
    verified_at_ms: 1_753_800_000_000,
    explanation: 'No Signal Program build proof supplied.',
  },

  updated_at_ms: 1_753_800_000_000,
  revision: 1,
  market_pulse: {
    session: 'OPEN',
    market_state: 'TRADABLE',
    market_liveness_reason: 'Fresh test evidence proves tradability.',
    market_liveness_observed_at_ms: 1_700_000_001_000,
    halted_symbol: null,
    feed_state: 'LIVE',
    latest_bar_at_ms: 1_700_000_000_000,
    age_ms: 1_000,
    source: 'ibkr',
    expected_cadence_ms: 60_000,
    headline: 'Market data live',
    explanation: 'The feed is current.',
    next_step: null,
    attention_required: false,
    observed_at_ms: 1_700_000_001_000,
  },
  feed_continuity: {
    provider_label: 'IBKR market data',
    run_id: 'run-1',
    state: 'continuous',
    state_label: 'Continuous',
    explanation: 'No IBKR delivery interruptions have been recorded in this run.',
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
    evaluated_at_ms: 1_753_800_000_000,
  },
  execution_policy: 'Observation only.',
  health: {
    strategy_instance_id: 'sid-001',
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
    account_id: 'DUM284968',
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
  journal_tail_ref: '/api/brokers/alpaca/clerks/clrk_spec/accounts/DUM284968/bots/sid-001/journal',
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
  realized_pnl_today: 0.0,
  open_pnl: null,
  open_pnl_usd: null,
  open_pnl_direction: null,
};

const SAFE_FLATTEN_PLAN: SqliteSafeFlattenPlan = {
  version_token: 'plan-token-17',
  account_id: 'DUM284968',
  authority_generation: 4,
  db_identity_token: 'db-generation-4',
  control_revision: 17,
  scope: 'CUSTODY_SUBJECT',
  strategy_instance_id: 'sid-001',
  reconciliation_id: 'reconciliation-17',
  account_check_kind: 'reconcile_now',
  account_check_observed_at_ms: 1_753_799_999_500,
  prepared_at_ms: 1_753_800_000_000,
  expires_at_ms: 4_102_444_800_000,
  legs: [{
    strategy_instance_id: 'sid-001',
    symbol: 'QQQ',
    side: 'sell',
    quantity: 2.5,
    position_updated_at_ms: 1_753_799_999_000,
  }],
};

const PREPARE_SAFE_FLATTEN_ACTION = {
  action_id: 'prepare_safe_flatten',
  revision: 17,
  concurrency_token: 'plan-token-17',
  enabled: true,
  label: 'Prepare safe flatten',
  explanation: 'Prepare a fresh reduction plan without submitting an order.',
  blockers: [],
  confirmation: null,
} satisfies PanelAction;

const HISTORICAL_RECOVERY_ACTION = {
  action_id: 'recover_exact_execution_evidence',
  revision: 17,
  concurrency_token: 'historical-token-17',
  enabled: true,
  label: 'Recover exact execution evidence',
  explanation: 'Read the retained Alpaca paper execution before resolving coverage.',
  blockers: [],
  confirmation: null,
} satisfies PanelAction;

const OPEN_CUSTODY_TIMELINE_ACTION = {
  action_id: 'open_custody_timeline',
  revision: 17,
  concurrency_token: 'timeline-token-17',
  enabled: true,
  label: 'Open custody timeline',
  explanation: 'Inspect the exact immutable custody evidence.',
  blockers: [],
  confirmation: null,
  evidence_refs: [
    'uncertainty:uncertainty:17',
    'order:learn-ai/qqq/1',
    'execution:alpaca-execution-1',
    'operation:effect:sid-001:recovery',
  ],
} satisfies PanelAction;

const HISTORICAL_RECOVERY_PLAN: HistoricalExecutionRecoveryPlan = {
  account_id: 'DUM284968',
  strategy_instance_id: 'sid-001',
  uncertainty_id: 'uncertainty:historical-1',
  order_ref: 'learn-ai/qqq/1',
  broker_order_id: 'alpaca-order-1',
  execution_id: 'alpaca-execution-1',
  exact_symbol: 'QQQ',
  exact_quantity: 2.5,
  exact_price: 481.42,
  exact_side: 'BUY',
  source_event_at_ms: 1_753_800_000_000,
  cumulative_fill_id: 'learn-ai/qqq/1:2.500000000',
  cumulative_quantity: 2.5,
  cumulative_price: 481.42,
  cumulative_side: 'BUY',
  authority_generation: 4,
  db_identity_token: 'db-generation-4',
  control_revision: 17,
  prepared_at_ms: 1_753_800_000_000,
  expires_at_ms: 1_753_800_120_000,
  confirmation_token: 'confirmation-token',
};

const SAFE_FLATTEN_CAPABILITY: SqliteRecoveryAction = {
  action_id: 'prepare_safe_flatten',
  label: 'Prepare safe flatten',
  explanation: 'Prepare a fresh reduction plan without submitting an order.',
  available: true,
  unavailable_reason_code: null,
  unavailable_reason: null,
  scope: 'CUSTODY_SUBJECT',
  freshness: 'fresh',
  evidence: [],
  reduction_plan: SAFE_FLATTEN_PLAN,
  confirmation: null,
  next_step: 'Review the exact attributed quantity in the plan.',
  concurrency_token: 'plan-token-17',
  execution_ref: null,
  mutation: false,
  primary: false,
};

const LIVE_CHART = {
  strategy_instance_id: 'sid-001',
  symbol: 'QQQ',
  trading_date_open_ms: 1_753_800_000_000,
  trading_date_close_ms: 1_753_823_400_000,
  resolution: '5s' as const,
  bars: [],
  fill_markers: [],
  overlay_notices: [],
  feed: fakeChartFeed(),
  as_of_ms: 1_753_800_000_000,
};

function liveSnapshot(panel: BotPanelView = PANEL): BotPanelLiveSnapshot {
  return {
    stream_epoch: 'test-epoch',
    surface_version: 1,
    // What the backend leads the page with, for this panel's own actions and state (#2794).
    panel: panel.bot_page ? panel : { ...panel, bot_page: fakeBotPage(panel) },
    live_chart: LIVE_CHART,
  };
}

function safeFlattenSnapshot(): BotPanelLiveSnapshot {
  return liveSnapshot({
    ...PANEL,
    revision: 17,
    actions: [PREPARE_SAFE_FLATTEN_ACTION],
    primary_action: 'prepare_safe_flatten',
    readiness_checks: [{
      operation: PREPARE_SAFE_FLATTEN_ACTION.action_id,
      label: PREPARE_SAFE_FLATTEN_ACTION.label,
      ready: true,
      scope: 'bot',
      authority: 'Alpaca SQLite Clerk',
      explanation: PREPARE_SAFE_FLATTEN_ACTION.explanation,
      evidence: { primary: true },
      evaluated_at_ms: 1_753_800_000_000,
      cure: null,
    }],
    readiness_ready_count: 1,
  });
}

/** The unpriced Execute safe flatten, as the adapter presents it in PRE/POST (#2007). */
const EXECUTE_SAFE_FLATTEN_ACTION = {
  action_id: 'execute_safe_flatten',
  revision: 17,
  concurrency_token: 'execute-token-17',
  enabled: false,
  label: 'Execute safe flatten',
  explanation: 'Submit the prepared reduction as recovery EXIT custody.',
  blockers: [],
  confirmation: null,
} satisfies PanelAction;

const ENABLED_EXECUTE = { ...EXECUTE_SAFE_FLATTEN_ACTION, enabled: true, concurrency_token: 'execute-token-18' };

/** An unconfirmed command as the SQLite panel presents it: the one button the
 * command tests click, since a real stop (`fakeSqliteStopAction`) confirms first. */
const RECONCILE_ACTION = {
  action_id: 'reconcile_now', revision: 17, concurrency_token: 'reconcile-token-17', enabled: true,
  label: 'Reconcile now', explanation: 'Reconcile this bot now.', blockers: [], confirmation: null,
} satisfies PanelAction;

/** A crashed bot still holding 2.5 QQQ, with no header action (the backend's
 * rule): the stranded-position warning is where it is flattened (H30–H31). */
function strandedPanel(overrides: Partial<BotPanelView> = {}): BotPanelView {
  return {
    ...PANEL,
    mode: 'trade',
    revision: 17,
    exposure: { QQQ: 2.5 },
    health: {
      ...PANEL.health,
      running: false,
      desired_state: 'STOPPED',
      desired_state_label: 'Stopped',
      duty_outcome: {
        kind: 'CRASHED', reason_code: 'FEED_DEATH', label: 'Crashed: market data stopped',
        explanation: 'The market-data feed stopped delivering bars.',
        recorded_at_ms: 1_753_800_000_000, run_id: 'run-current', exposure_notices: [],
      },
    },
    actions: [RECONCILE_ACTION],
    primary_action: null,
    ...overrides,
  };
}

/** The stranded bot as the extended-hours flatten sees it: Prepare re-mints
 * each pass, and Execute waits unpriced until the owner sets a limit (#2007). */
function extendedFlattenSnapshot(prepareToken = 'plan-token-17'): BotPanelLiveSnapshot {
  return liveSnapshot(strandedPanel({
    actions: [
      RECONCILE_ACTION,
      { ...PREPARE_SAFE_FLATTEN_ACTION, concurrency_token: prepareToken },
      EXECUTE_SAFE_FLATTEN_ACTION,
    ],
  }));
}

function historicalRecoverySnapshot(): BotPanelLiveSnapshot {
  return liveSnapshot({
    ...PANEL,
    revision: 17,
    actions: [HISTORICAL_RECOVERY_ACTION],
    primary_action: 'recover_exact_execution_evidence',
    readiness_checks: [{
      operation: HISTORICAL_RECOVERY_ACTION.action_id,
      label: HISTORICAL_RECOVERY_ACTION.label,
      ready: true,
      scope: 'bot',
      authority: 'Alpaca SQLite Clerk',
      explanation: HISTORICAL_RECOVERY_ACTION.explanation,
      evidence: { primary: true },
      evaluated_at_ms: 1_753_800_000_000,
      cure: 'Prepare exact Alpaca paper evidence for this conflict.',
    }],
    readiness_ready_count: 1,
  });
}

function custodyTimelineSnapshot(): BotPanelLiveSnapshot {
  return liveSnapshot({
    ...PANEL,
    revision: 17,
    actions: [OPEN_CUSTODY_TIMELINE_ACTION],
    primary_action: 'open_custody_timeline',
    readiness_checks: [{
      operation: OPEN_CUSTODY_TIMELINE_ACTION.action_id,
      label: OPEN_CUSTODY_TIMELINE_ACTION.label,
      ready: true,
      scope: 'bot',
      authority: 'Alpaca SQLite Clerk',
      explanation: OPEN_CUSTODY_TIMELINE_ACTION.explanation,
      evidence: { primary: true },
      evaluated_at_ms: 1_753_800_000_000,
      cure: null,
    }],
    readiness_ready_count: 1,
  });
}

function deferred<T>(): { promise: Promise<T>; resolve(value: T): void } {
  let resolvePromise: (value: T) => void = () => undefined;
  const promise = new Promise<T>((resolve) => {
    resolvePromise = resolve;
  });
  return { promise, resolve: resolvePromise };
}

class StubEventSource {
  static latest: StubEventSource | null = null;
  addEventListener = vi.fn();
  close = vi.fn();

  constructor(readonly url: string) {
    StubEventSource.latest = this;
  }

  emit(name: string, data: string): void {
    for (const [eventName, listener] of this.addEventListener.mock.calls) {
      if (eventName === name) (listener as (event: MessageEvent<string>) => void)(
        new MessageEvent(name, { data }),
      );
    }
  }
}

function emitOnLiveStream(name: string, data: string): void {
  const source = StubEventSource.latest;
  if (source === null) throw new Error('No live stream was opened.');
  source.emit(name, data);
}

const originalEventSource = globalThis.EventSource;
(globalThis as { EventSource?: unknown }).EventSource = StubEventSource;

function makeRun(overrides: Partial<BotRunView> = {}): BotRunView {
  return {
    strategy_instance_id: 'sid-001',
    run_id: 'run-current',
    configuration_hash: 'a'.repeat(64),
    launch_reason: 'deploy',
    started_at_ms: 1_753_800_000_000,
    process: {
      strategy_instance_id: 'sid-001',
      run_id: 'run-current',
      process_identity: 'process-1',
      state: 'RUNNING',
      registry_generation: 'registry-1',
      observed_at_ms: 1_753_800_005_000,
    },
    terminal_outcome: null,
    ...overrides,
  };
}

const BUDGET_RESULTS = [
  { label: 'Budget set aside at deploy', amount_usd: '1000.00' },
  { label: 'Realized gains and losses', amount_usd: '0.00' },
  { label: 'Fees', amount_usd: '-0.01' },
  { label: 'Balance', amount_usd: '999.99', total: true },
];

const RUNNING_BUDGET: DeploymentBudgetView = {
  state: 'ready',
  headline: 'Holding its position',
  detail: 'Its next entry waits until this position is sold.',
  strategy_instance_id: 'sid-001',
  world: 'real_paper',
  observed_at_ms: 1_753_800_000_000,
  segment: {
    kind: 'bot', strategy_instance_id: 'sid-001', label: 'sid-001', amount_usd: '999.99', share_bps: 10000, palette_index: 0,
    parts: {
      in_shares_usd: '764.71', in_shares_bps: 7647,
      pending_usd: '0.00', pending_bps: 0,
      free_usd: '235.28', free_bps: 2353,
    },
  },
  statement: [
    ...BUDGET_RESULTS,
    { label: 'In shares, at cost', amount_usd: '764.71' },
    { label: 'Waiting in entry orders', amount_usd: '0.00' },
    { label: 'Free to trade', amount_usd: '235.28' },
  ],
  note: 'The budget limits new entries. Market fills and losses can go past it.',
};

const STOPPED_BUDGET: DeploymentBudgetView = {
  ...RUNNING_BUDGET,
  headline: 'Stopped · still holds shares',
  detail: 'Its free budget was released when it stopped. The money in its shares comes back when they are sold.',
  segment: {
    kind: 'stopped', strategy_instance_id: 'sid-001', label: 'held by stopped bot sid-001', amount_usd: '764.71',
    share_bps: 10000, released_usd: '235.28', still_claimed_usd: '0.00', palette_index: 0,
  },
  statement: [
    ...BUDGET_RESULTS,
    { label: 'Released at stop', amount_usd: '235.28' },
    { label: 'Still in shares, at cost', amount_usd: '764.71' },
    { label: 'Still in entry orders', amount_usd: '0.00' },
  ],
  note: null,
};

const mockService = {
  getBudget: vi.fn().mockResolvedValue(RUNNING_BUDGET),
  getPanel: vi.fn().mockResolvedValue(PANEL),
  getLiveSnapshot: vi.fn().mockResolvedValue(liveSnapshot()),
  liveStreamUrl: vi.fn().mockReturnValue('/api/test/live-stream'),
  getCurrentRun: vi.fn().mockResolvedValue(makeRun()),
  // No decision bars yet: the strategy tab's calm empty state, no chart.
  getStrategyView: vi.fn().mockResolvedValue(fakeStrategyView({ candles: [] })),
  getHistoryChart: vi.fn().mockResolvedValue({
    strategy_instance_id: 'sid-001',
    symbol: 'QQQ',
    timeframe: '1m',
    from_ms: 1_753_800_000_000,
    to_ms: 1_753_823_400_000,
    bars: [],
    fill_markers: [],
    truncated: false,
    as_of_ms: 1_753_800_000_000,
  }),
  getEvidence: vi.fn().mockResolvedValue({
    strategy_instance_id: 'sid-001',
    account_id: 'DUM284968',
    transaction_ref: 'tx-001',
    entries: [
      {
        seq: 1,
        kind: 'submit_acked',
        kind_label: 'Submit acknowledged',
        recorded_at_ms: 1_753_800_000_000,
        order_ref: 'tx-001',
        intent_id: 'intent-001',
        summary: 'The broker acknowledged the order.',
        has_more_detail: false,
      },
    ],
    next_cursor: null,
    total_entries: 1,
    truncated: false,
    read_at_ms: 1_753_800_000_000,
  }),
  runBotAction: vi.fn().mockResolvedValue({
    action_id: 'reconcile_now',
    outcome: 'success',
    receipt_id: 'receipt-001',
    recorded_at_ms: 1_753_800_000_000,
    applied: true,
    revision: 17,
    concurrency_token: 'reconcile-token-17',
    message: 'Reconciliation requested.',
  }),
  executeExtendedSafeFlatten: vi.fn().mockResolvedValue({
    action_id: 'execute_safe_flatten',
    outcome: 'success',
    applied: true,
    receipt_id: 'order-ref-flatten-1',
    recorded_at_ms: 1_753_800_000_200,
    command: null,
    reconciliation: null,
    orders: [{
      order_ref: 'order-ref-flatten-1',
      client_order_id: 'order-ref-flatten-1',
      broker_order_id: 'alp-flatten-1',
      role: 'REDUCING',
      broker_state: 'accepted',
      submitted_at_ms: 1_753_800_000_150,
      updated_at_ms: 1_753_800_000_200,
    }],
  }),
  prepareHistoricalExecutionRecovery: vi.fn().mockResolvedValue(HISTORICAL_RECOVERY_PLAN),
  confirmHistoricalExecutionRecovery: vi.fn().mockResolvedValue({
    uncertainty_id: HISTORICAL_RECOVERY_PLAN.uncertainty_id,
    order_ref: HISTORICAL_RECOVERY_PLAN.order_ref,
    execution_id: HISTORICAL_RECOVERY_PLAN.execution_id,
    receipt_id: 'coverage-resolution:18',
    recorded_at_ms: 1_753_800_000_100,
    applied: true,
  }),
};

const brokersMock = {
  checkSqliteRecoveryActionInFull: vi.fn().mockResolvedValue({
    capability: SAFE_FLATTEN_CAPABILITY,
    reduction_pricing: { kind: 'regular_session' },
  } satisfies SqliteRecoveryActionCheck),
};

function openDisclosure(label: string): void {
  const details = screen.getByText(label).closest('details');
  if (details === null) throw new Error(`Expected ${label} disclosure.`);
  details.open = true;
  fireEvent(details, new Event('toggle'));
}

/** The chart panel opens on the strategy view (#2639); the market tape is one
 * tab away and is only created when that tab first opens. */
async function showTape(fixture: ComponentFixture<BotPanelShellComponent>): Promise<void> {
  fireEvent.click(screen.getByRole('tab', { name: /^Tape · / }));
  await fixture.whenStable();
  fixture.detectChanges();
}

/** The current-run poll's interval on a virtual clock, so a test lets one poll
 * come due without waiting it out in wall-clock time (#2592). Only intervals
 * are virtual: Angular's scheduler and the service promises keep real time. */
function virtualRunPoll(): { elapse(): Promise<void> } {
  vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
  onTestFinished(() => {
    vi.useRealTimers();
  });
  return {
    async elapse() {
      await vi.advanceTimersByTimeAsync(CURRENT_RUN_POLL_MS + 100);
    },
  };
}

function fakeActionResult(overrides: Partial<PanelActionResult> = {}): PanelActionResult {
  return {
    action_id: 'reconcile_now',
    outcome: 'success',
    receipt_id: 'receipt-001',
    recorded_at_ms: 1_753_800_000_000,
    applied: true,
    revision: 17,
    concurrency_token: 'reconcile-token-17',
    message: 'Reconciliation requested.',
    ...overrides,
  };
}

/** The workspace's header as production gives it to the bot page: the slot's controls drawn above the page. */
@Component({
  selector: 'app-header-slot-host',
  imports: [BotPanelShellComponent, NgTemplateOutlet],
  providers: [WorkspaceHeaderSlot],
  template: `
    <header>
      @if (slot.content(); as controls) {
        <ng-container [ngTemplateOutlet]="controls" />
      }
    </header>
    <app-bot-panel-shell broker="alpaca" clerkId="clrk_spec" accountId="DUM284968" sid="sid-001" />
  `,
})
class HeaderSlotHostComponent {
  protected readonly slot = inject(WorkspaceHeaderSlot);
}

/** Renders the shell with a single unconfirmed command (Reconcile now) in its
 * header, so the fence tests only need to click one button. */
async function renderShell(
  overrides: {
    directory?: FleetDirectoryDouble;
    runBotAction?: ReturnType<typeof vi.fn>;
    /** The bot's end on its page (#2607), and the service's change of it. */
    end?: BotEndView;
    editBotEnd?: ReturnType<typeof vi.fn>;
  } = {},
) {
  // A persistent mock, not `mockResolvedValueOnce`: a rebind restarts the
  // live store (its address is a read and must follow the current lane, see
  // the constructor's second effect), which re-fetches the snapshot. A local
  // override — not a mutation of the shared `mockService.getLiveSnapshot` —
  // keeps every fetch, including that restart-triggered one, returning the
  // one-command panel without leaking a persistent mock into later tests.
  const service = {
    ...mockService,
    getLiveSnapshot: vi.fn().mockResolvedValue(liveSnapshot({
      ...PANEL,
      actions: [RECONCILE_ACTION],
      primary_action: 'reconcile_now',
      ...(overrides.end ? { end: overrides.end } : {}),
    })),
    ...(overrides.runBotAction ? { runBotAction: overrides.runBotAction } : {}),
    ...(overrides.editBotEnd ? { editBotEnd: overrides.editBotEnd } : {}),
  };
  const { fixture } = await render(BotPanelShellComponent, {
    inputs: { clerkId: 'clrk_spec', broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
    providers: [
      provideRouter([]),
      { provide: BrokerV2PanelService, useValue: service },
      { provide: BrokersService, useValue: brokersMock },
      { provide: MessageService, useValue: messageService },
      ...(overrides.directory
        ? [{ provide: overrides.directory.provide, useValue: overrides.directory.useValue }]
        : []),
    ],
  });
  await fixture.whenStable();
  fixture.detectChanges();
  return { fixture, service };
}

describe('BotPanelShellComponent', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
  });

  afterAll(() => {
    globalThis.EventSource = originalEventSource;
  });

  describe('the workspace tab this page belongs to', () => {
    // A bot's page sits inside the account workspace, under Home (PRD #2560):
    // Overview, Bots and Gallery merged into it, so there is no tab to stamp.
    async function renderShellWithStamp(query: string) {
      const view = await render(BotPanelShellComponent, {
        inputs: { clerkId: 'clrk_spec', broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
        providers: [
          provideRouter([{ path: '**', children: [] }]),
          { provide: BrokerV2PanelService, useValue: mockService },
          { provide: BrokersService, useValue: brokersMock },
          { provide: MessageService, useValue: messageService },
        ],
      });
      await TestBed.inject(Router).navigateByUrl(
        `/brokers/alpaca/clerks/clrk_spec/accounts/DUM284968/bots/sid-001${query}`,
      );
      await view.fixture.whenStable();
      view.fixture.detectChanges();
      return view;
    }

    it.each([[''], ['?from=gallery']])('points its way back at Home (%s)', async (query) => {
      await renderShellWithStamp(query);

      const back = screen.getByRole('link', { name: 'Home' });
      expect(back.getAttribute('href')).toBe('/brokers/alpaca/clerks/clrk_spec/accounts/DUM284968');
    });
  });

  describe('while the live producer is stalled (#2353)', () => {
    const STALL = {
      reason: 'PRODUCER_STALLED',
      message: 'The live panel stopped updating.',
      why: 'The data plane has not completed a panel refresh in over 20 seconds.',
      next_action: 'The values shown are frozen; the controls still work.',
      last_produced_at_ms: 1_753_800_000_000,
      observed_at_ms: 1_753_800_060_000,
    } as const;

    function oneCommandSnapshot(): BotPanelLiveSnapshot {
      return liveSnapshot({
        ...PANEL,
        health: { ...PANEL.health, running: false },
        actions: [RECONCILE_ACTION],
        primary_action: 'reconcile_now',
      });
    }

    async function renderShell(): Promise<ComponentFixture<BotPanelShellComponent>> {
      const { fixture } = await render(BotPanelShellComponent, {
        inputs: { clerkId: 'clrk_spec', broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
        providers: [provideRouter([]), { provide: BrokerV2PanelService, useValue: mockService }, { provide: BrokersService, useValue: brokersMock },
          { provide: MessageService, useValue: messageService }],
      });
      await fixture.whenStable();
      fixture.detectChanges();
      return fixture;
    }

    it('shows the server notice above the frozen panel and keeps its controls', async () => {
      mockService.getLiveSnapshot.mockResolvedValueOnce(oneCommandSnapshot());
      const fixture = await renderShell();

      emitOnLiveStream('stale', JSON.stringify(STALL));
      await fixture.whenStable();
      fixture.detectChanges();

      const notice = screen.getByRole('alert', { name: 'The live panel stopped updating.' });
      expect(within(notice).getByText(STALL.next_action)).toBeTruthy();
      expect(within(notice).getByText(
        formatTimestampDisplay(STALL.last_produced_at_ms, { mode: 'local' }),
        { exact: false },
      )).toBeTruthy();
      expect(fixture.nativeElement.classList.contains('is-stale')).toBe(true);
      // The bot's status and figures, drawn in the workspace header, go grey too.
      expect(fixture.nativeElement.querySelector('.bot-page__controls').classList.contains('is-stale')).toBe(true);
      await showTape(fixture);
      expect(screen.getByRole('article', { name: 'Market tape for QQQ' })).toBeTruthy();
      expect(screen.getByRole('button', { name: 'Check against Alpaca' })).toBeTruthy();

      emitOnLiveStream('snapshot', JSON.stringify(oneCommandSnapshot()));
      await fixture.whenStable();
      fixture.detectChanges();

      expect(screen.queryByRole('alert', { name: 'The live panel stopped updating.' })).toBeNull();
      expect(fixture.nativeElement.classList.contains('is-stale')).toBe(false);
      expect(fixture.nativeElement.querySelector('.bot-page__controls').classList.contains('is-stale')).toBe(false);
    });

    it('keeps the action receipt when the post-action refresh finds the producer stalled', async () => {
      mockService.getLiveSnapshot
        .mockResolvedValueOnce(oneCommandSnapshot())
        .mockRejectedValueOnce(new HttpErrorResponse({ status: 503, error: { detail: STALL } }));
      const fixture = await renderShell();

      fireEvent.click(screen.getByRole('button', { name: 'Check against Alpaca' }));
      for (let step = 0; step < 4; step += 1) {
        await fixture.whenStable();
        fixture.detectChanges();
      }

      expect(mockService.getLiveSnapshot).toHaveBeenCalledTimes(2);
      expect(screen.getByRole('alert', { name: 'The live panel stopped updating.' })).toBeTruthy();
      expect(screen.getByText('Reconciliation requested.')).toBeTruthy();
      expect(screen.getByText('receipt-001')).toBeTruthy();
      expect(screen.getByRole('button', { name: 'Check against Alpaca' })).toBeTruthy();
    });
  });

  describe("while only the chart's own line is down (#2355)", () => {
    it('shows the chart-line notice and the backend headline, not the producer stall', async () => {
      const chartDown: BotPanelLiveSnapshot = {
        ...liveSnapshot({
          ...PANEL,
          market_pulse: { ...PANEL.market_pulse, headline: 'Bot market data live' },
        }),
        live_chart: {
          ...LIVE_CHART,
          feed: fakeChartFeed({
            state: 'STALLED',
            headline: 'Chart feed stalled',
            explanation: "The chart's IBKR bar line has not delivered a bar within its expected cadence.",
            next_step: 'Do not read the chart as current.',
            show_notice: true,
            attention_required: true,
            last_bar_at_ms: 1_753_800_000_000,
          }),
        },
      };
      mockService.getLiveSnapshot.mockResolvedValueOnce(chartDown);
      const { fixture } = await render(BotPanelShellComponent, {
        inputs: { clerkId: 'clrk_spec', broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
        providers: [provideRouter([]), { provide: BrokerV2PanelService, useValue: mockService }, { provide: BrokersService, useValue: brokersMock },
          { provide: MessageService, useValue: messageService }],
      });
      await fixture.whenStable();
      fixture.detectChanges();
      await showTape(fixture);

      const notice = screen.getByRole('alert', { name: 'Chart feed stalled' });
      expect(within(notice).getByText('Do not read the chart as current.')).toBeTruthy();
      // The headline speaks for the bot's own line; the chart speaks for its own.
      expect(screen.getByText('Bot market data live')).toBeTruthy();
      expect(screen.queryByRole('alert', { name: 'The live panel stopped updating.' })).toBeNull();
      expect(fixture.nativeElement.classList.contains('is-stale')).toBe(false);
    });
  });

  it('shows loading state initially then renders the one view', async () => {
    const { fixture } = await render(BotPanelShellComponent, {
      inputs: { clerkId: 'clrk_spec', broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
      providers: [provideRouter([]), { provide: BrokerV2PanelService, useValue: mockService }, { provide: BrokersService, useValue: brokersMock },
        { provide: MessageService, useValue: messageService }],
    });

    // Wait for async initial load
    await fixture.whenStable();
    fixture.detectChanges();

    // The loaded panel drives the chart's instrument context.
    expect(screen.getByRole('tab', { name: 'Strategy · 15m', selected: true })).toBeTruthy();
    await showTape(fixture);
    expect(screen.getByRole('article', { name: 'Market tape for QQQ' })).toBeTruthy();
    expect(mockService.getLiveSnapshot).toHaveBeenCalledWith(
      expect.objectContaining({
        broker: 'alpaca', clerkId: 'clrk_spec', accountId: 'DUM284968', entityId: 'sid-001',
      }),
      'sid-001',
      '5s',
    );
    expect(screen.queryByText('run-current')).toBeNull();
    expect(mockService.getCurrentRun).toHaveBeenCalledTimes(1);
    // The backend's summary line names the run, never a ticking time; the status opens it.
    await userEvent.click(screen.getByRole('button', { name: 'Running' }));
    expect(screen.getByText(/^Running since /)).toBeTruthy();


    openDisclosure('Runs');
    await fixture.whenStable();
    fixture.detectChanges();

    expect(screen.getByText('run-current')).toBeTruthy();
  });

  describe('every safe-flatten trigger routes into the warning (H30)', () => {
    async function renderWith(snapshot: BotPanelLiveSnapshot) {
      const view = await render(BotPanelShellComponent, {
        inputs: { clerkId: 'clrk_spec', broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
        providers: [
          provideRouter([]),
          { provide: BrokerV2PanelService, useValue: { ...mockService, getLiveSnapshot: vi.fn().mockResolvedValue(snapshot) } },
          { provide: BrokersService, useValue: brokersMock },
          { provide: MessageService, useValue: messageService },
        ],
      });
      await view.fixture.whenStable();
      view.fixture.detectChanges();
      return view;
    }

    /** The toolbar's Sell: one button for the two-step ticket (#2794 R6). */
    async function clickToolbarSell(fixture: ComponentFixture<BotPanelShellComponent>): Promise<void> {
      const toolbar = screen.getByRole('toolbar', { name: 'Actions for this bot' });
      fireEvent.click(within(toolbar).getByRole('button', { name: /^Sell/ }));
      await fixture.whenStable();
      fixture.detectChanges();
    }

    it('opens the warning’s confirmation from the toolbar’s Sell and sends nothing', async () => {
      const panel = strandedPanel();
      const { fixture } = await renderWith(liveSnapshot({ ...panel, actions: [...panel.actions, PREPARE_SAFE_FLATTEN_ACTION] }));

      await clickToolbarSell(fixture);

      await vi.waitFor(() => expect(document.activeElement?.textContent?.trim()).toBe('Sell 2.5 QQQ'));
      expect(screen.getByRole('group', { name: 'Sell 2.5 QQQ?' })).toBeTruthy();
      expect(brokersMock.checkSqliteRecoveryActionInFull).not.toHaveBeenCalled();
      expect(mockService.runBotAction).not.toHaveBeenCalled();
    });

    it('refuses a safe flatten of a running bot and sends nothing', async () => {
      const { fixture } = await renderWith(safeFlattenSnapshot());

      await clickToolbarSell(fixture);

      const outcome = await screen.findByRole('alert', { name: 'Action outcome' });
      expect(outcome.textContent).toContain(
        'Flatten is offered for a stopped bot holding one position, from the warning at the top of its page.',
      );
      expect(screen.queryByRole('region', { name: 'Prepared safe-flatten reduction plan' })).toBeNull();
      expect(brokersMock.checkSqliteRecoveryActionInFull).not.toHaveBeenCalled();
      expect(mockService.runBotAction).not.toHaveBeenCalled();
    });

    it('keeps the warning’s Flatten for a stopped bot still holding shares', async () => {
      await renderWith(liveSnapshot(strandedPanel()));

      const warning = screen.getByRole('region', { name: /No bot is managing 2.5 QQQ/ });
      expect(within(warning).getByRole('button', { name: 'Flatten…' }).hasAttribute('disabled')).toBe(false);
    });
  });

  it('prepares and explicitly confirms historical exact-execution recovery', async () => {
    mockService.getLiveSnapshot.mockResolvedValueOnce(historicalRecoverySnapshot());
    const { fixture } = await render(BotPanelShellComponent, {
      inputs: { clerkId: 'clrk_spec', broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
      providers: [
        provideRouter([]),
        { provide: BrokerV2PanelService, useValue: mockService },
        { provide: BrokersService, useValue: brokersMock },
        { provide: MessageService, useValue: messageService },
      ],
    });
    await fixture.whenStable();
    fixture.detectChanges();

    // The backend policy folds RecoveryCapability.primary into the Operator
    // reference (ADR 0027 precedence, #1665): the banner renders it once,
    // and the readiness accordion suppresses its own would-be duplicate row.
    expect(screen.getAllByRole('button', { name: 'Recover a missing fill' })).toHaveLength(1);
    fireEvent.click(screen.getByRole('button', { name: 'Recover a missing fill' }));
    await fixture.whenStable();
    fixture.detectChanges();

    expect(mockService.prepareHistoricalExecutionRecovery).toHaveBeenCalledWith(
      expect.objectContaining({
        broker: 'alpaca', clerkId: 'clrk_spec', accountId: 'DUM284968', entityId: 'sid-001',
      }),
      'sid-001',
      'historical-token-17',
    );
    expect(screen.getByRole('heading', { name: 'Confirm exact execution recovery' })).toBeTruthy();
    expect(screen.getByText(/alpaca-execution-1 records BUY 2.5 at/i)).toBeTruthy();
    expect(
      fixture.nativeElement.querySelector('app-typed-halt-confirm app-asset-identity')?.textContent,
    ).toContain('QQQ');
    expect(screen.getByRole('dialog', { name: 'Confirm exact execution recovery' }).textContent).toContain('DUM284968');

    fireEvent.input(screen.getByTestId('typed-halt-confirm-input'), {
      target: { value: 'RECOVER' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Recover exact evidence' }));
    await fixture.whenStable();
    fixture.detectChanges();

    expect(mockService.confirmHistoricalExecutionRecovery).toHaveBeenCalledWith(
      expect.objectContaining({
        broker: 'alpaca', clerkId: 'clrk_spec', accountId: 'DUM284968', entityId: 'sid-001',
      }),
      'sid-001',
      HISTORICAL_RECOVERY_PLAN,
    );
    // The fleet ledger pins an idempotency key to one operation: confirm must not reuse prepare's,
    // and must address the same lane the operator was shown.
    const prepared = mockService.prepareHistoricalExecutionRecovery.mock.calls[0][0];
    const confirmed = mockService.confirmHistoricalExecutionRecovery.mock.calls[0][0];
    expect(prepared.idempotencyKey).toEqual(expect.any(String));
    expect(confirmed.idempotencyKey).toEqual(expect.any(String));
    expect(confirmed.idempotencyKey).not.toBe(prepared.idempotencyKey);
    expect({ ...confirmed, idempotencyKey: null }).toEqual({ ...prepared, idempotencyKey: null });
    expect(screen.getByText(/recorded exact evidence without changing economic totals/i)).toBeTruthy();
    expect(mockService.runBotAction).not.toHaveBeenCalledWith(
      expect.objectContaining({
        broker: 'alpaca', clerkId: 'clrk_spec', accountId: 'DUM284968', entityId: 'sid-001',
      }),
      'sid-001',
      HISTORICAL_RECOVERY_ACTION,
      null,
    );
  });

  it('opens this bot’s order records on Activity with one exact, non-cross-correlated evidence identity', async () => {
    mockService.getLiveSnapshot.mockResolvedValueOnce(custodyTimelineSnapshot());
    const { fixture } = await render(BotPanelShellComponent, {
      inputs: { clerkId: 'clrk_spec', broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
      providers: [
        provideRouter([]),
        { provide: BrokerV2PanelService, useValue: mockService },
        { provide: BrokersService, useValue: brokersMock },
        { provide: MessageService, useValue: messageService },
      ],
    });
    const navigate = vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
    await fixture.whenStable();
    fixture.detectChanges();

    // Inspect's actions wait under More.
    fireEvent.click(screen.getByRole('button', { name: /^More actions/ }));
    fixture.detectChanges();
    fireEvent.click(screen.getByRole('button', { name: 'Custody timeline' }));

    const queryParams = { timelineBot: 'sid-001', timelineUncertaintyId: 'uncertainty:17' };
    expect(navigate).toHaveBeenLastCalledWith(
      ['/brokers', 'alpaca', 'clerks', 'clrk_spec', 'accounts', 'DUM284968', 'activity'], { queryParams },
    );
    // Exactly the query Activity reads to open its records fold on this bot.
    expect(sqliteTimelineQueryFromParams(new URLSearchParams(queryParams)))
      .toEqual(expect.objectContaining({ strategyInstanceId: 'sid-001', uncertaintyId: 'uncertainty:17' }));
  });

  it('renders the server-authored stale-plan refusal and refreshes without confirmation', async () => {
    mockService.getLiveSnapshot.mockResolvedValueOnce(historicalRecoverySnapshot());
    mockService.prepareHistoricalExecutionRecovery.mockRejectedValueOnce(
      new HttpErrorResponse({
        status: 409,
        error: {
          detail: {
            reason: 'stale_action_token',
            message: 'The recovery evidence changed. Refresh before confirming the action.',
          },
        },
      }),
    );
    const { fixture } = await render(BotPanelShellComponent, {
      inputs: { clerkId: 'clrk_spec', broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
      providers: [
        provideRouter([]),
        { provide: BrokerV2PanelService, useValue: mockService },
        { provide: BrokersService, useValue: brokersMock },
        { provide: MessageService, useValue: messageService },
      ],
    });
    await fixture.whenStable();
    fixture.detectChanges();

    fireEvent.click(screen.getByRole('button', { name: 'Recover a missing fill' }));
    await fixture.whenStable();
    fixture.detectChanges();

    expect(screen.getByText('The recovery evidence changed. Refresh before confirming the action.')).toBeTruthy();
    expect(screen.getByText('Stale Action Token')).toBeTruthy();
    expect(screen.queryByRole('heading', { name: 'Confirm exact execution recovery' })).toBeNull();
  });

  it('discards a flatten’s checked plan after route identity changes', async () => {
    const pendingCapability = deferred<SqliteRecoveryActionCheck>();
    brokersMock.checkSqliteRecoveryActionInFull.mockReturnValueOnce(pendingCapability.promise);
    const getPanel = vi.fn()
      .mockResolvedValueOnce(strandedPanel())
      .mockResolvedValueOnce(strandedPanel({ actions: [RECONCILE_ACTION, PREPARE_SAFE_FLATTEN_ACTION] }));
    const { fixture } = await render(BotPanelShellComponent, {
      inputs: { clerkId: 'clrk_spec', broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
      providers: [
        provideRouter([]),
        {
          provide: BrokerV2PanelService,
          useValue: { ...mockService, getLiveSnapshot: vi.fn().mockResolvedValue(extendedFlattenSnapshot()), getPanel },
        },
        { provide: BrokersService, useValue: brokersMock },
        { provide: MessageService, useValue: messageService },
      ],
    });
    await fixture.whenStable();
    fixture.detectChanges();

    await userEvent.click(screen.getByRole('button', { name: 'Flatten…' }));
    await userEvent.click(within(screen.getByRole('group', { name: 'Sell 2.5 QQQ?' })).getByRole('button', { name: 'Sell 2.5 QQQ' }));
    await vi.waitFor(() => expect(brokersMock.checkSqliteRecoveryActionInFull).toHaveBeenCalledTimes(1));

    fixture.componentRef.setInput('sid', 'sid-002');
    fixture.detectChanges();
    pendingCapability.resolve({
      capability: SAFE_FLATTEN_CAPABILITY,
      reduction_pricing: {
        kind: 'extended_limit', phase: 'PRE', symbol: 'QQQ', side: 'sell',
        bid: 480.1, ask: 480.2, bid_size: 300, ask_size: 200,
        quote_observed_at_ms: Date.now(), quote_max_age_ms: 10_000,
        exit_allowance_bps: 20, suggested_limit_price: 479.13, band_limit_price: 478.17,
        spread: 0.1, spread_bps: 2.08, wide_spread: false, spread_warning_bps: 50, proposal: null,
      },
    });
    await fixture.whenStable();
    fixture.detectChanges();

    expect(screen.queryByRole('region', {
      name: 'Prepared safe-flatten reduction plan',
    })).toBeNull();
    expect(screen.queryByRole('region', { name: 'Extended-hours flatten limit order' })).toBeNull();
  });

  describe('action outcome ownership (#2471)', () => {
    /** Re-bind the always-rendered page instance to another identity, the way
     * the attention bell and browser history do, and let a pending command land. */
    async function rebindAndResolve(
      fixture: ComponentFixture<BotPanelShellComponent>,
      pending: { promise: Promise<PanelActionResult>; resolve(value: PanelActionResult): void },
      input: { sid?: string; accountId?: string; clerkId?: string },
    ): Promise<void> {
      const [[key, value]] = Object.entries(input);
      fixture.componentRef.setInput(key, value);
      await fixture.whenStable();
      fixture.detectChanges();
      pending.resolve(fakeActionResult());
      await fixture.whenStable();
      fixture.detectChanges();
    }

    it('clears a finished outcome when the page rebinds to another bot', async () => {
      const { fixture } = await renderShell({
        runBotAction: vi.fn().mockResolvedValue(fakeActionResult()),
      });
      fireEvent.click(screen.getByRole('button', { name: 'Check against Alpaca' }));
      await fixture.whenStable();
      fixture.detectChanges();
      expect(screen.getByText('Reconciliation requested.')).toBeTruthy();
      expect(screen.getByText('receipt-001')).toBeTruthy();

      fixture.componentRef.setInput('sid', 'sid-002');
      await fixture.whenStable();
      fixture.detectChanges();

      expect(screen.queryByText('Reconciliation requested.')).toBeNull();
      expect(screen.queryByText('receipt-001')).toBeNull();
    });

    it('keeps a late command off the new bot, sends exactly one command to the old bot, and names it', async () => {
      const pending = deferred<PanelActionResult>();
      const runBotAction = vi.fn().mockReturnValueOnce(pending.promise);
      const { fixture } = await renderShell({ runBotAction });

      fireEvent.click(screen.getByRole('button', { name: 'Check against Alpaca' }));
      await fixture.whenStable();
      fixture.detectChanges();

      await rebindAndResolve(fixture, pending, { sid: 'sid-002' });

      expect(screen.queryByText('Reconciliation requested.')).toBeNull();
      expect(screen.queryByText('receipt-001')).toBeNull();
      expect(runBotAction).toHaveBeenCalledTimes(1);
      expect(runBotAction.mock.calls[0]?.[1]).toBe('sid-001');
      const toast = messageService.add.mock.calls.at(-1)?.[0] as { detail: string };
      expect(toast.detail).toContain('Ema Crossover');
      expect(toast.detail).toContain('Reconciliation requested.');
    });

    it('keeps a late command off the panel when the account changes', async () => {
      const pending = deferred<PanelActionResult>();
      const runBotAction = vi.fn().mockReturnValueOnce(pending.promise);
      const { fixture } = await renderShell({ runBotAction });

      fireEvent.click(screen.getByRole('button', { name: 'Check against Alpaca' }));
      await fixture.whenStable();
      fixture.detectChanges();

      await rebindAndResolve(fixture, pending, { accountId: 'DUM284969' });

      expect(screen.queryByText('Reconciliation requested.')).toBeNull();
      expect(runBotAction).toHaveBeenCalledTimes(1);
    });

    it('keeps a late command off the panel when the lane changes', async () => {
      const pending = deferred<PanelActionResult>();
      const runBotAction = vi.fn().mockReturnValueOnce(pending.promise);
      const { fixture } = await renderShell({ runBotAction });

      fireEvent.click(screen.getByRole('button', { name: 'Check against Alpaca' }));
      await fixture.whenStable();
      fixture.detectChanges();

      await rebindAndResolve(fixture, pending, { clerkId: 'clrk_other' });

      expect(screen.queryByText('Reconciliation requested.')).toBeNull();
      expect(runBotAction).toHaveBeenCalledTimes(1);
    });
  });

  describe('an extended-hours safe flatten (#2007)', () => {
    /** Let a multi-step flow finish: a send refreshes the panel, posts, then refreshes again. */
    async function settle(fixture: ComponentFixture<BotPanelShellComponent>): Promise<void> {
      for (let step = 0; step < 4; step += 1) {
        await fixture.whenStable();
        fixture.detectChanges();
      }
    }

    afterEach(() => {
      // These specs point the shared doubles at a stranded bot; every later
      // spec reads the defaults.
      mockService.getLiveSnapshot.mockReset().mockResolvedValue(liveSnapshot());
      mockService.getPanel.mockReset().mockResolvedValue(PANEL);
      brokersMock.checkSqliteRecoveryActionInFull.mockReset().mockResolvedValue({
        capability: SAFE_FLATTEN_CAPABILITY,
        reduction_pricing: { kind: 'regular_session' },
      });
    });

    /** Flatten the stranded bot from its warning, the one way a flatten
     * starts: reconcile, a fresh panel, then the checked plan (H30). */
    async function prepareFlatten(): Promise<ComponentFixture<BotPanelShellComponent>> {
      mockService.getPanel
        .mockResolvedValueOnce(strandedPanel())
        .mockResolvedValueOnce(strandedPanel({ actions: [RECONCILE_ACTION, PREPARE_SAFE_FLATTEN_ACTION] }));
      const { fixture } = await render(BotPanelShellComponent, {
        inputs: { clerkId: 'clrk_spec', broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
        providers: [
          provideRouter([]),
          { provide: BrokerV2PanelService, useValue: mockService },
          { provide: BrokersService, useValue: brokersMock },
          { provide: MessageService, useValue: messageService },
        ],
      });
      await fixture.whenStable();
      fixture.detectChanges();
      await userEvent.click(screen.getByRole('button', { name: 'Flatten…' }));
      await userEvent.click(within(screen.getByRole('group', { name: 'Sell 2.5 QQQ?' })).getByRole('button', { name: 'Sell 2.5 QQQ' }));
      await vi.waitFor(() => expect(brokersMock.checkSqliteRecoveryActionInFull).toHaveBeenCalledTimes(1));
      await settle(fixture);
      return fixture;
    }

    /** Each step's label and state, then the backend's words for it, as read aloud. */
    function progress(): string {
      const list = screen.getByRole('list', { name: 'Flatten progress' });
      return [...list.querySelectorAll('li > *')].map((part) => part.textContent?.trim()).join(' ');
    }

    function outcomeText(role: 'alert' | 'status'): string {
      return screen.getByRole(role, { name: 'Action outcome' }).textContent?.replace(/\s+/g, ' ') ?? '';
    }

    function flattenButton(): HTMLButtonElement {
      return screen.getByRole('button', { name: 'Flatten…' });
    }

    /** The Clerk's pricing, carrying its reading of the suggested price. */
    const EXTENDED_CHECK_WITH_READING = (observedAtMs: number): SqliteRecoveryActionCheck => ({
      capability: SAFE_FLATTEN_CAPABILITY,
      reduction_pricing: {
        kind: 'extended_limit', phase: 'PRE', symbol: 'QQQ', side: 'sell',
        bid: 480.1, ask: 480.2, bid_size: 300, ask_size: 200,
        quote_observed_at_ms: observedAtMs, quote_max_age_ms: 10_000,
        exit_allowance_bps: 20, suggested_limit_price: 479.13, band_limit_price: 478.17,
        spread: 0.1, spread_bps: 2.08, wide_spread: false, spread_warning_bps: 50,
        proposal: {
          limit_price: 479.13, through_book_bps: 20.2, worst_case_cost: 2.43,
          outside_band: false, thin_book: false, resting: false,
        },
      },
    });

    it('shows the live IBKR quote and sends the confirmed limit through the custody route', async () => {
      const observedAtMs = Date.now();
      mockService.getLiveSnapshot.mockResolvedValue(extendedFlattenSnapshot());
      brokersMock.checkSqliteRecoveryActionInFull.mockResolvedValueOnce({
        capability: SAFE_FLATTEN_CAPABILITY,
        reduction_pricing: {
          kind: 'extended_limit',
          phase: 'PRE',
          symbol: 'QQQ',
          side: 'sell',
          bid: 480.1,
          ask: 480.2,
          bid_size: 300,
          ask_size: 200,
          quote_observed_at_ms: observedAtMs,
          quote_max_age_ms: 10_000,
          exit_allowance_bps: 20,
          suggested_limit_price: 479.13,
          band_limit_price: 478.17,
          spread: 0.1,
          spread_bps: 2.08,
          wide_spread: false,
          spread_warning_bps: 50,
          proposal: null,
        },
      } satisfies SqliteRecoveryActionCheck);
      // Review asks the Clerk to read the operator's price; its answer is what
      // the confirm pane shows and what Send sends.
      brokersMock.checkSqliteRecoveryActionInFull.mockResolvedValueOnce({
        capability: SAFE_FLATTEN_CAPABILITY,
        reduction_pricing: {
          kind: 'extended_limit',
          phase: 'PRE',
          symbol: 'QQQ',
          side: 'sell',
          bid: 480.1,
          ask: 480.2,
          bid_size: 300,
          ask_size: 200,
          quote_observed_at_ms: observedAtMs,
          quote_max_age_ms: 10_000,
          exit_allowance_bps: 20,
          suggested_limit_price: 479.13,
          band_limit_price: 478.17,
          spread: 0.1,
          spread_bps: 2.08,
          wide_spread: false,
          spread_warning_bps: 50,
          proposal: {
            limit_price: 479.13,
            through_book_bps: 20.2,
            worst_case_cost: 2.43,
            outside_band: false,
            thin_book: false,
            resting: false,
          },
        },
      } satisfies SqliteRecoveryActionCheck);
      const fixture = await prepareFlatten();

      const ticket = await screen.findByRole('region', { name: 'Extended-hours flatten limit order' });
      // The Sell step waits on the ticket, and no second flatten can start over it.
      expect(progress()).toContain('Sell: In progress');
      expect(flattenButton().disabled).toBe(true);
      expect(within(ticket).getByText('Pre-market')).toBeTruthy();
      expect(within(ticket).getByText(/\$480\.10/)).toBeTruthy();
      expect(within(ticket).getByText(/\$480\.20/)).toBeTruthy();
      fireEvent.click(within(ticket).getByRole('button', { name: 'Review limit order' }));
      await settle(fixture);
      expect(within(ticket).getByText(/at limit \$479\.13/)).toBeTruthy();
      expect(brokersMock.checkSqliteRecoveryActionInFull).toHaveBeenLastCalledWith(
        expect.anything(),
        expect.anything(),
        expect.objectContaining({ proposed_limit_price: 479.13 }),
        expect.anything(),
      );
      fireEvent.click(within(ticket).getByRole('button', { name: 'Send limit order' }));
      await settle(fixture);

      expect(mockService.executeExtendedSafeFlatten).toHaveBeenCalledWith(
        expect.objectContaining({ clerkId: 'clrk_spec', accountId: 'DUM284968' }),
        'sid-001',
        'execute-token-17',
        { limit_price: 479.13, quote_observed_at_ms: observedAtMs },
      );
      // Only the reconcile went through a plain action: the sale went through the ticket.
      expect(mockService.runBotAction.mock.calls.map((call: unknown[]) => (call[2] as PanelAction).action_id))
        .toEqual(['reconcile_now']);
      expect(screen.queryByRole('region', { name: 'Prepared safe-flatten reduction plan' }))
        .toBeNull();
      expect(outcomeText('status')).toMatch(/Limit order sent at \$479\.13/);
      // The ticket's outcome settles the Sell step, and Flatten is offered again.
      expect(progress()).toContain('Sell: Done Limit order sent at $479.13');
      await vi.waitFor(() => expect(flattenButton().disabled).toBe(false));
    });

    it('fails the Sell step when the limit order is refused', async () => {
      const observedAtMs = Date.now();
      mockService.getLiveSnapshot.mockResolvedValue(extendedFlattenSnapshot());
      brokersMock.checkSqliteRecoveryActionInFull.mockImplementation(async () => EXTENDED_CHECK_WITH_READING(observedAtMs));
      mockService.executeExtendedSafeFlatten.mockRejectedValueOnce(new HttpErrorResponse({
        status: 409,
        error: { detail: { reason: 'stale_action_token', message: 'The flatten changed before it was sent.' } },
      }));
      const fixture = await prepareFlatten();
      const ticket = await screen.findByRole('region', { name: 'Extended-hours flatten limit order' });
      fireEvent.click(within(ticket).getByRole('button', { name: 'Review limit order' }));
      await settle(fixture);

      fireEvent.click(within(ticket).getByRole('button', { name: 'Send limit order' }));
      await settle(fixture);

      expect(progress()).toContain('Sell: Failed The flatten changed before it was sent.');
      expect(outcomeText('alert')).toContain('The flatten changed before it was sent.');
      // A typed refusal says the order was not placed, so the plan still says none was submitted.
      expect(screen.getByRole('region', { name: 'Prepared safe-flatten reduction plan' }).textContent)
        .toContain('No order has been submitted.');
      await vi.waitFor(() => expect(flattenButton().disabled).toBe(false));
    });

    it.each([
      ['a dropped connection', new HttpErrorResponse({ status: 0, error: new ProgressEvent('error') })],
      // The coordinator's own word for a command it dispatched and could not settle.
      ['a lane that failed after dispatch', new HttpErrorResponse({
        status: 503,
        error: {
          reason: 'clerk_routing_outcome_unknown',
          message: 'Clerk clrk_spec failed serving custody_bot_recovery_execute with 502 after the dispatch.',
          next_step: 'The command may have been applied. Reconcile by the idempotency key.',
        },
      })],
    ])('stops saying no order was submitted after %s (#3178)', async (_name, rejection) => {
      const observedAtMs = Date.now();
      mockService.getLiveSnapshot.mockResolvedValue(extendedFlattenSnapshot());
      brokersMock.checkSqliteRecoveryActionInFull.mockImplementation(async () => EXTENDED_CHECK_WITH_READING(observedAtMs));
      mockService.executeExtendedSafeFlatten.mockRejectedValueOnce(rejection);
      const fixture = await prepareFlatten();
      const ticket = await screen.findByRole('region', { name: 'Extended-hours flatten limit order' });
      fireEvent.click(within(ticket).getByRole('button', { name: 'Review limit order' }));
      await settle(fixture);

      fireEvent.click(within(ticket).getByRole('button', { name: 'Send limit order' }));
      await settle(fixture);

      const plan = screen.getByRole('region', { name: 'Prepared safe-flatten reduction plan' });
      expect(plan.textContent).not.toContain('No order has been submitted');
      expect(plan.textContent).toContain('is not known');
      if (_name === 'a lane that failed after dispatch') {
        expect(outcomeText('alert')).toContain('Unknown');
      }
    });

    it('keeps saying no order was submitted when the request never went out (#3178)', async () => {
      const observedAtMs = Date.now();
      mockService.getLiveSnapshot.mockResolvedValue(extendedFlattenSnapshot());
      brokersMock.checkSqliteRecoveryActionInFull.mockImplementation(async () => EXTENDED_CHECK_WITH_READING(observedAtMs));
      const fixture = await prepareFlatten();
      const ticket = await screen.findByRole('region', { name: 'Extended-hours flatten limit order' });
      fireEvent.click(within(ticket).getByRole('button', { name: 'Review limit order' }));
      await settle(fixture);
      // A newer panel no longer presents the flatten, so the send throws before any request is built.
      const live = extendedFlattenSnapshot();
      mockService.getLiveSnapshot.mockResolvedValue({
        ...live,
        surface_version: live.surface_version + 1,
        panel: {
          ...live.panel,
          actions: live.panel.actions.filter((action) => action.action_id !== 'execute_safe_flatten'),
        },
      });

      fireEvent.click(within(ticket).getByRole('button', { name: 'Send limit order' }));
      await settle(fixture);

      expect(mockService.executeExtendedSafeFlatten).not.toHaveBeenCalled();
      expect(screen.getByRole('region', { name: 'Prepared safe-flatten reduction plan' }).textContent)
        .toContain('No order has been submitted.');
    });

    it('stops saying Pricing… when the Clerk’s price check fails, and names why beside the ticket', async () => {
      const observedAtMs = Date.now();
      mockService.getLiveSnapshot.mockResolvedValue(extendedFlattenSnapshot());
      brokersMock.checkSqliteRecoveryActionInFull.mockImplementation(async () => EXTENDED_CHECK_WITH_READING(observedAtMs));
      const fixture = await prepareFlatten();
      const ticket = await screen.findByRole('region', { name: 'Extended-hours flatten limit order' });
      // From here the Clerk cannot price the operator's own limit.
      brokersMock.checkSqliteRecoveryActionInFull.mockRejectedValue(new HttpErrorResponse({
        status: 503,
        error: { detail: { message: 'The Clerk could not read its quote.', why: null, next_action: null } },
      }));
      fireEvent.input(within(ticket).getByLabelText('Limit price (USD)'), { target: { value: '478.50' } });

      fireEvent.click(within(ticket).getByRole('button', { name: 'Review limit order' }));
      await settle(fixture);

      expect(within(ticket).queryByRole('button', { name: 'Pricing…' })).toBeNull();
      expect((within(ticket).getByRole('button', { name: 'Review limit order' }) as HTMLButtonElement).disabled).toBe(false);
      expect(screen.getByText(/Quote refresh failed: The Clerk could not read its quote\./)).toBeTruthy();
      expect(mockService.executeExtendedSafeFlatten).not.toHaveBeenCalled();
    });

    it('closes the ticket and fails the Sell step when a refresh finds the plan gone', async () => {
      mockService.getLiveSnapshot.mockResolvedValue(extendedFlattenSnapshot());
      brokersMock.checkSqliteRecoveryActionInFull.mockResolvedValueOnce(EXTENDED_CHECK_WITH_READING(Date.now()));
      const fixture = await prepareFlatten();
      brokersMock.checkSqliteRecoveryActionInFull.mockResolvedValueOnce({
        capability: {
          ...SAFE_FLATTEN_CAPABILITY,
          available: false,
          reduction_plan: null,
          unavailable_reason_code: 'NO_ATTRIBUTED_POSITION',
          unavailable_reason: 'This bot no longer holds a position to sell.',
          next_step: 'Nothing is left to flatten.',
        },
        reduction_pricing: null,
      } satisfies SqliteRecoveryActionCheck);

      const ticket = await screen.findByRole('region', { name: 'Extended-hours flatten limit order' });
      fireEvent.click(within(ticket).getByRole('button', { name: 'Refresh quote' }));
      await settle(fixture);

      expect(screen.queryByRole('region', { name: 'Extended-hours flatten limit order' })).toBeNull();
      expect(progress()).toContain('Sell: Failed This bot no longer holds a position to sell.');
      const outcome = outcomeText('alert');
      expect(outcome).toContain('No Attributed Position');
      expect(outcome).toContain('Nothing is left to flatten.');
      expect(document.activeElement?.classList.contains('action-receipt')).toBe(true);
      expect(flattenButton().disabled).toBe(false);
    });

    it('takes the execute token from a live panel read while the live projection is stalled (#2353)', async () => {
      const observedAtMs = Date.now();
      mockService.getLiveSnapshot.mockResolvedValue(extendedFlattenSnapshot());
      brokersMock.checkSqliteRecoveryActionInFull.mockImplementation(async () => EXTENDED_CHECK_WITH_READING(observedAtMs));
      const fixture = await prepareFlatten();
      const ticket = await screen.findByRole('region', { name: 'Extended-hours flatten limit order' });
      fireEvent.click(within(ticket).getByRole('button', { name: 'Review limit order' }));
      await settle(fixture);

      mockService.getLiveSnapshot.mockRejectedValue(new HttpErrorResponse({
        status: 503,
        error: {
          detail: {
            reason: 'PRODUCER_STALLED',
            message: 'The live panel stopped updating.',
            why: 'The data plane has not completed a panel refresh in over 20 seconds.',
            next_action: 'The values shown are frozen; the controls still work.',
            last_produced_at_ms: 1_753_800_000_000,
            observed_at_ms: 1_753_800_060_000,
          },
        },
      }));
      const livePanel = extendedFlattenSnapshot().panel;
      mockService.getPanel.mockResolvedValueOnce({
        ...livePanel,
        actions: livePanel.actions.map((action) => action.action_id === 'execute_safe_flatten'
          ? { ...action, concurrency_token: 'execute-token-live' }
          : action),
      });
      fireEvent.click(within(ticket).getByRole('button', { name: 'Send limit order' }));
      await settle(fixture);
      await settle(fixture);

      expect(screen.getByRole('alert', { name: 'The live panel stopped updating.' })).toBeTruthy();
      expect(mockService.executeExtendedSafeFlatten).toHaveBeenCalledWith(
        expect.objectContaining({ clerkId: 'clrk_spec', accountId: 'DUM284968' }),
        'sid-001',
        'execute-token-live',
        { limit_price: 479.13, quote_observed_at_ms: observedAtMs },
      );
      expect(outcomeText('status')).toMatch(/Limit order sent at \$479\.13/);
    });

    it('never says an order was sent when none reached the broker', async () => {
      // A durably accepted EXIT whose reducing order is still pending — a
      // broker lookup outage, a transiently blocked REDUCE — has sent nothing,
      // and telling the operator otherwise is the one thing worse than the
      // outage itself (Codex review 2026-09-19).
      const observedAtMs = Date.now();
      mockService.getLiveSnapshot.mockResolvedValue(extendedFlattenSnapshot());
      brokersMock.checkSqliteRecoveryActionInFull.mockImplementation(async () => EXTENDED_CHECK_WITH_READING(observedAtMs));
      mockService.executeExtendedSafeFlatten.mockResolvedValueOnce({
        action_id: 'execute_safe_flatten',
        outcome: 'success',
        applied: true,
        receipt_id: 'effect-flatten-2',
        recorded_at_ms: 1_753_800_000_200,
        command: null,
        reconciliation: null,
        orders: [],
      });
      const fixture = await prepareFlatten();

      const ticket = await screen.findByRole('region', { name: 'Extended-hours flatten limit order' });
      fireEvent.click(within(ticket).getByRole('button', { name: 'Review limit order' }));
      await settle(fixture);
      fireEvent.click(within(ticket).getByRole('button', { name: 'Send limit order' }));
      await settle(fixture);

      expect(outcomeText('status')).toMatch(/no order has reached the broker yet/);
      expect(progress()).toMatch(/Sell: Done Flatten accepted at \$479\.13, but no order has reached the broker yet/);
      expect(screen.queryByText(/Limit order sent/)).toBeNull();
    });

    it('recovers from a rotated Prepare token instead of dead-ending the ticket', async () => {
      // The Clerk re-mints the token every reconciliation pass, so a refresh
      // that meets a rotated one must refresh the panel and retry with the
      // token it now presents — never strand the ticket (#2007 review).
      mockService.getLiveSnapshot.mockResolvedValueOnce(extendedFlattenSnapshot());
      mockService.getLiveSnapshot.mockResolvedValue(extendedFlattenSnapshot('plan-token-18'));
      const extendedCheck = {
        capability: SAFE_FLATTEN_CAPABILITY,
        reduction_pricing: {
          kind: 'extended_limit', phase: 'PRE', symbol: 'QQQ', side: 'sell',
          bid: 480.1, ask: 480.2, bid_size: 300, ask_size: 200,
          quote_observed_at_ms: Date.now(), quote_max_age_ms: 10_000,
          exit_allowance_bps: 20, suggested_limit_price: 479.13,
          band_limit_price: 478.17, spread: 0.1, spread_bps: 2.08, wide_spread: false,
          spread_warning_bps: 50, proposal: null,
        },
      } satisfies SqliteRecoveryActionCheck;
      brokersMock.checkSqliteRecoveryActionInFull.mockResolvedValueOnce(extendedCheck);
      const fixture = await prepareFlatten();
      brokersMock.checkSqliteRecoveryActionInFull.mockRejectedValueOnce(
        new HttpErrorResponse({
          status: 409,
          error: { detail: { reason: 'stale_action_token', message: 'The plan changed.' } },
        }),
      );
      brokersMock.checkSqliteRecoveryActionInFull.mockResolvedValueOnce(extendedCheck);

      const ticket = await screen.findByRole('region', { name: 'Extended-hours flatten limit order' });
      fireEvent.click(within(ticket).getByRole('button', { name: 'Refresh quote' }));
      await settle(fixture);

      // Prepare, the refused refresh, then the retry — each with whatever
      // token the panel presented at the time.
      expect(brokersMock.checkSqliteRecoveryActionInFull).toHaveBeenCalledTimes(3);
      const tokens = brokersMock.checkSqliteRecoveryActionInFull.mock.calls.map(
        (call: unknown[]) => (call[2] as { concurrency_token: string }).concurrency_token,
      );
      // Each call carries the token the panel presented at the time. The
      // retry's *value* cannot be asserted here: this harness's live store
      // does not push the second snapshot into the signal the shell reads, so
      // the rotation itself is covered by the refresh path, not by this spec.
      expect(tokens[1]).toBe('plan-token-17');
      expect(tokens[2]).toBeDefined();
      expect(mockService.getLiveSnapshot.mock.calls.length).toBeGreaterThan(1);
      expect(screen.queryByText(/Quote refresh failed/)).toBeNull();
      expect(screen.getByRole('region', { name: 'Extended-hours flatten limit order' })).toBeTruthy();
    });

    it.each([
      ['NO_SESSION_OPEN', 1_753_862_400_000, 'No Session Open'],
      ['SIMULATED_RECOVERY_PRICE_UNAVAILABLE', null, 'Simulated Recovery Price Unavailable'],
    ] as const)('names a refused sale’s code (%s) and when the next session opens, instead of a ticket', async (code, opensAt, label) => {
      mockService.getLiveSnapshot.mockResolvedValue(extendedFlattenSnapshot());
      brokersMock.checkSqliteRecoveryActionInFull.mockResolvedValueOnce({
        capability: SAFE_FLATTEN_CAPABILITY,
        reduction_pricing: {
          kind: 'refused',
          reason_code: code,
          explanation: 'No sale can be priced now, so nothing was sent.',
          next_step: 'Flatten again later.',
          available_at_ms: opensAt,
        },
      } satisfies SqliteRecoveryActionCheck);
      await prepareFlatten();

      await screen.findByRole('alert', { name: 'Action outcome' });
      const outcome = outcomeText('alert');
      expect(outcome).toContain(label);
      expect(outcome).toContain('No sale can be priced now, so nothing was sent.');
      if (opensAt === null) {
        expect(outcome).not.toContain('Next session opens');
      } else {
        expect(outcome).toContain(`Next session opens ${formatTimestampDisplay(opensAt, { mode: 'local' })}`);
      }
      expect(progress()).toContain('Sell: Failed');
      expect(screen.queryByRole('region', { name: 'Extended-hours flatten limit order' })).toBeNull();
    });

    it('fails the check step, with the backend’s words, when Reconcile is refused', async () => {
      mockService.getLiveSnapshot.mockResolvedValue(extendedFlattenSnapshot());
      mockService.getPanel.mockResolvedValueOnce(strandedPanel());
      mockService.runBotAction.mockRejectedValueOnce(new HttpErrorResponse({
        status: 409,
        error: { detail: { reason: 'stale_action_token', message: 'The custody state changed.', why: 'Try again.' } },
      }));
      const { fixture } = await render(BotPanelShellComponent, {
        inputs: { clerkId: 'clrk_spec', broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
        providers: [
          provideRouter([]),
          { provide: BrokerV2PanelService, useValue: mockService },
          { provide: BrokersService, useValue: brokersMock },
          { provide: MessageService, useValue: messageService },
        ],
      });
      await fixture.whenStable();
      fixture.detectChanges();

      await userEvent.click(screen.getByRole('button', { name: 'Flatten…' }));
      await userEvent.click(within(screen.getByRole('group', { name: 'Sell 2.5 QQQ?' })).getByRole('button', { name: 'Sell 2.5 QQQ' }));

      await vi.waitFor(() => expect(document.activeElement?.classList.contains('action-receipt')).toBe(true));
      expect(document.activeElement?.textContent).toContain('Stale Action Token');
      expect(document.activeElement?.textContent).toContain('The custody state changed.');
      expect(progress()).toContain('Check the position with Alpaca: Failed The custody state changed.');
      expect(brokersMock.checkSqliteRecoveryActionInFull).not.toHaveBeenCalled();
      // The sequence stops there: Reconcile is not sent again, and the page is
      // re-read so the next Flatten runs on what the Clerk shows now (#2874).
      expect(mockService.runBotAction).toHaveBeenCalledTimes(1);
      await vi.waitFor(() => expect(mockService.getLiveSnapshot).toHaveBeenCalledTimes(2));
    });
  });

  it('renders one view: the banner, the toolbar and the six-panel board, with no Trader/Operator switch', async () => {
    const { fixture, container } = await render(BotPanelShellComponent, {
      inputs: { clerkId: 'clrk_spec', broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
      providers: [
        provideRouter([]),
        { provide: BrokerV2PanelService, useValue: mockService },
        { provide: BrokersService, useValue: brokersMock },
        { provide: MessageService, useValue: messageService },
      ],
    });
    await fixture.whenStable();
    fixture.detectChanges();

    expect(container.querySelectorAll('app-bot-page-header')).toHaveLength(1);
    expect(screen.getByRole('toolbar', { name: 'Actions for this bot' })).toBeTruthy();
    expect(screen.queryByRole('tab', { name: 'Trader' })).toBeNull();
    expect(screen.queryByRole('tab', { name: 'Operator' })).toBeNull();
    // The chart on the left; on the right, money, orders and setup, then decisions and health.
    const panels = (selector: string): string[] =>
      [...container.querySelectorAll(selector)].map((panel) => panel.classList[1]);
    expect(panels('.bot-board > .bot-board__panel')).toEqual(['bot-board__chart']);
    expect(panels('.bot-board__column:first-child > .bot-board__panel'))
      .toEqual(['bot-board__money', 'bot-board__orders', 'bot-board__setup']);
    expect(panels('.bot-board__column:last-child > .bot-board__panel'))
      .toEqual(['bot-board__decisions', 'bot-board__health']);
  });

  it('runs an action from the controls it drew in the workspace header', async () => {
    const service = {
      ...mockService,
      getLiveSnapshot: vi.fn().mockResolvedValue(liveSnapshot({
        ...PANEL, actions: [RECONCILE_ACTION], primary_action: 'reconcile_now',
      })),
    };
    const { fixture, container } = await render(HeaderSlotHostComponent, {
      providers: [
        provideRouter([]),
        { provide: BrokerV2PanelService, useValue: service },
        { provide: BrokersService, useValue: brokersMock },
        { provide: MessageService, useValue: messageService },
      ],
    });
    await fixture.whenStable();
    fixture.detectChanges();

    const header = container.querySelector('header');
    if (!(header instanceof HTMLElement)) throw new Error('the host header did not render');
    await userEvent.click(await within(header).findByRole('button', { name: 'Check against Alpaca' }));
    await fixture.whenStable();

    expect(service.runBotAction).toHaveBeenCalledTimes(1);
    expect(container.querySelector('app-bot-panel-shell app-bot-toolbar')).toBeNull();
  });

  it('hands its name, figures and actions to the workspace header, and takes them back when it goes', async () => {
    const slot = new WorkspaceHeaderSlot();
    const { fixture, container } = await render(BotPanelShellComponent, {
      inputs: { clerkId: 'clrk_spec', broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
      providers: [
        provideRouter([]),
        { provide: WorkspaceHeaderSlot, useValue: slot },
        { provide: BrokerV2PanelService, useValue: mockService },
        { provide: BrokersService, useValue: brokersMock },
        { provide: MessageService, useValue: messageService },
      ],
    });
    await fixture.whenStable();
    fixture.detectChanges();

    expect(slot.content()).not.toBeNull();
    expect(container.querySelector('app-bot-page-header')).toBeNull();
    expect(screen.queryByRole('toolbar', { name: 'Actions for this bot' })).toBeNull();

    fixture.destroy();
    expect(slot.content()).toBeNull();
  });

  it('prices the tape from the last IBKR bar and never reads a Polygon snapshot (H15)', async () => {
    const bar = (startMs: number, close: string) => ({
      start_ms: startMs, end_ms: startMs + 5_000, open: close, high: close, low: close, close,
      volume: 10, source: 'ibkr' as const,
    });
    const service = {
      ...mockService,
      getLiveSnapshot: vi.fn().mockResolvedValue({
        ...liveSnapshot(),
        live_chart: { ...LIVE_CHART, bars: [bar(1_753_800_000_000, '511.10'), bar(1_753_800_005_000, '512.34')] },
      }),
    };
    const { fixture } = await render(BotPanelShellComponent, {
      inputs: { clerkId: 'clrk_spec', broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
      providers: [
        provideRouter([]),
        { provide: BrokerV2PanelService, useValue: service },
        { provide: BrokersService, useValue: brokersMock },
        { provide: MessageService, useValue: messageService },
      ],
    });
    await fixture.whenStable();
    fixture.detectChanges();
    await showTape(fixture);

    const tape = screen.getByRole('article', { name: 'Market tape for QQQ' });
    expect(within(tape).getByText('$512.34')).toBeTruthy();
    expect(within(tape).getByText('IBKR · last bar')).toBeTruthy();
    expect(within(tape).queryByText(/Polygon snapshot/)).toBeNull();
  });

  it("shows the current run under Runs and sends earlier runs to History, narrowed to this bot", async () => {
    const { fixture } = await render(BotPanelShellComponent, {
      inputs: { clerkId: 'clrk_spec', broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
      providers: [
        provideRouter([]),
        { provide: BrokerV2PanelService, useValue: mockService },
        { provide: BrokersService, useValue: brokersMock },
        { provide: MessageService, useValue: messageService },
      ],
    });
    await fixture.whenStable();
    openDisclosure('Runs');
    await fixture.whenStable();
    fixture.detectChanges();

    expect(screen.getByText('run-current')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Previous Runs' })).toBeNull();
    expect(screen.getByRole('link', { name: 'History' }).getAttribute('href'))
      .toBe('/brokers/alpaca/clerks/clrk_spec/history?account=clrk_spec&bot=sid-001');
    expect(mockService.getCurrentRun).toHaveBeenCalledTimes(1);
  });

  it('keeps lifecycle actions bound to the current instance with Runs open', async () => {
    mockService.getLiveSnapshot.mockResolvedValueOnce(liveSnapshot({
      ...PANEL,
      health: { ...PANEL.health, running: false },
      actions: [RECONCILE_ACTION],
      primary_action: 'reconcile_now',
    }));
    const { fixture } = await render(BotPanelShellComponent, {
      inputs: { clerkId: 'clrk_spec', broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
      providers: [
        provideRouter([]),
        { provide: BrokerV2PanelService, useValue: mockService },
        { provide: BrokersService, useValue: brokersMock },
        { provide: MessageService, useValue: messageService },
      ],
    });
    await fixture.whenStable();
    openDisclosure('Runs');
    await fixture.whenStable();
    fixture.detectChanges();

    fireEvent.click(screen.getByRole('button', { name: 'Check against Alpaca' }));
    await fixture.whenStable();

    expect(mockService.runBotAction).toHaveBeenCalledWith(
      expect.objectContaining({
        broker: 'alpaca', clerkId: 'clrk_spec', accountId: 'DUM284968', entityId: 'sid-001',
      }),
      'sid-001',
      expect.objectContaining({ action_id: 'reconcile_now' }),
      null,
    );
  });

  it('does not restart a panel request that is still loading', async () => {
    vi.useFakeTimers();
    const pendingPanel = new Promise<BotPanelLiveSnapshot>(() => undefined);
    const slowService = {
      ...mockService,
      getLiveSnapshot: vi.fn().mockReturnValue(pendingPanel),
    };

    try {
      const { fixture } = await render(BotPanelShellComponent, {
        inputs: { clerkId: 'clrk_spec', broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
        providers: [
          provideRouter([]),
          { provide: BrokerV2PanelService, useValue: slowService },
          { provide: BrokersService, useValue: brokersMock },
          { provide: MessageService, useValue: messageService },
        ],
      });
      fixture.detectChanges();

      expect(slowService.getLiveSnapshot).toHaveBeenCalledTimes(1);
      await vi.advanceTimersByTimeAsync(5_000);
      expect(slowService.getLiveSnapshot).toHaveBeenCalledTimes(1);
      fixture.destroy();
    } finally {
      vi.useRealTimers();
    }
  });

  it('refreshes current-run evidence during panel polling', async () => {
    const runPoll = virtualRunPoll();
    mockService.getCurrentRun
      .mockResolvedValueOnce(makeRun())
      .mockResolvedValueOnce(
        makeRun({
          process: { ...makeRun().process, state: 'EXITED', observed_at_ms: 1_753_805_000_000 },
          terminal_outcome: {
            kind: 'STOPPED',
            reason_code: 'OPERATOR_STOP',
            recorded_at_ms: 1_753_805_000_000,
            run_id: 'run-current',
          },
        }),
      );
    const { fixture } = await render(BotPanelShellComponent, {
      inputs: { clerkId: 'clrk_spec', broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
      providers: [
        provideRouter([]),
        { provide: BrokerV2PanelService, useValue: mockService },
        { provide: BrokersService, useValue: brokersMock },
        { provide: MessageService, useValue: messageService },
      ],
    });
    await fixture.whenStable();
    fixture.detectChanges();

    openDisclosure('Runs');
    await fixture.whenStable();
    fixture.detectChanges();
    expect(screen.getByText('No terminal evidence recorded')).toBeTruthy();
    await runPoll.elapse();
    await fixture.whenStable();
    fixture.detectChanges();

    expect(screen.getByText('Stopped')).toBeTruthy();
    fixture.destroy();
  });

  it('does not poll immutable current-run evidence while the bot is off duty', async () => {
    const runPoll = virtualRunPoll();
    mockService.getLiveSnapshot.mockResolvedValueOnce(liveSnapshot({
      ...PANEL,
      health: {
        ...PANEL.health,
        phase: 'OFF_DUTY',
        phase_label: 'Off duty',
        desired_state: 'STOPPED',
        desired_state_label: 'Stopped',
        running: false,
      },
    }));

    const { fixture } = await render(BotPanelShellComponent, {
      inputs: { clerkId: 'clrk_spec', broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
      providers: [
        provideRouter([]),
        { provide: BrokerV2PanelService, useValue: mockService },
        { provide: BrokersService, useValue: brokersMock },
        { provide: MessageService, useValue: messageService },
      ],
    });
    await fixture.whenStable();
    fixture.detectChanges();


    expect(mockService.getCurrentRun).toHaveBeenCalledTimes(1);
    openDisclosure('Runs');
    await fixture.whenStable();
    fixture.detectChanges();
    expect(mockService.getCurrentRun).toHaveBeenCalledTimes(1);
    await runPoll.elapse();
    expect(mockService.getCurrentRun).toHaveBeenCalledTimes(1);
    fixture.destroy();
  });

  it('shows log-only degradation panel after data loads', async () => {
    const { fixture } = await render(BotPanelShellComponent, {
      inputs: { clerkId: 'clrk_spec', broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
      providers: [provideRouter([]), { provide: BrokerV2PanelService, useValue: mockService }, { provide: BrokersService, useValue: brokersMock },
        { provide: MessageService, useValue: messageService }],
    });

    await fixture.whenStable();
    fixture.detectChanges();

    expect(
      screen.getByText('This bot records decisions but does not place broker orders.'),
    ).toBeTruthy();
  });

  it('shows an error message when panel load fails', async () => {
    mockService.getLiveSnapshot.mockRejectedValueOnce(new Error('Network error'));

    const { fixture } = await render(BotPanelShellComponent, {
      inputs: { clerkId: 'clrk_spec', broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
      providers: [provideRouter([]), { provide: BrokerV2PanelService, useValue: mockService }, { provide: BrokersService, useValue: brokersMock },
        { provide: MessageService, useValue: messageService }],
    });

    await fixture.whenStable();
    fixture.detectChanges();

    expect(screen.getByRole('alert').textContent).toBe('Network error');
  });

  it('fetches a new server projection for a selected transaction', async () => {
    const { fixture } = await render(BotPanelShellComponent, {
      inputs: { clerkId: 'clrk_spec', broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
      providers: [provideRouter([]), { provide: BrokerV2PanelService, useValue: mockService }, { provide: BrokersService, useValue: brokersMock },
        { provide: MessageService, useValue: messageService }],
    });
    await fixture.whenStable();

    // The audit trail is on the page, in the Orders panel (#2794).
    fixture.detectChanges();
    fireEvent.click(await screen.findByRole('button', { name: /Submit acknowledged at/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Select transaction tx-001 on rail' }));
    await fixture.whenStable();

    expect(mockService.getPanel).toHaveBeenLastCalledWith(
      expect.objectContaining({
        broker: 'alpaca', clerkId: 'clrk_spec', accountId: 'DUM284968', entityId: 'sid-001',
      }),
      'sid-001',
      'tx-001',
    );
  });

  it('renders the durable receipt returned by a bot action', async () => {
    mockService.getLiveSnapshot.mockResolvedValueOnce(liveSnapshot({
      ...PANEL,
      health: { ...PANEL.health, running: false },
      actions: [RECONCILE_ACTION],
      primary_action: 'reconcile_now',
    }));
    const { fixture } = await render(BotPanelShellComponent, {
      inputs: { clerkId: 'clrk_spec', broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
      providers: [provideRouter([]), { provide: BrokerV2PanelService, useValue: mockService }, { provide: BrokersService, useValue: brokersMock },
        { provide: MessageService, useValue: messageService }],
    });
    await fixture.whenStable();
    fixture.detectChanges();

    fireEvent.click(screen.getByRole('button', { name: 'Check against Alpaca' }));
    await fixture.whenStable();
    fixture.detectChanges();

    expect(screen.getByText('Reconciliation requested.')).toBeTruthy();
    expect(screen.getByText('receipt-001')).toBeTruthy();
    expect(messageService.add).toHaveBeenCalledWith(
      expect.objectContaining({ severity: 'success', detail: 'Reconciliation requested.' }),
    );
  });

  it('renders backend-authored remediation for an unknown action outcome', async () => {
    mockService.getLiveSnapshot.mockResolvedValueOnce(liveSnapshot({
      ...PANEL,
      health: { ...PANEL.health, running: false },
      actions: [RECONCILE_ACTION],
      primary_action: 'reconcile_now',
    }));
    mockService.runBotAction.mockRejectedValueOnce(
      new HttpErrorResponse({
        status: 500,
        error: {
          detail: {
            action_id: 'reconcile_now',
            outcome: 'unknown',
            receipt_id: 'receipt-unknown',
            recorded_at_ms: 1_753_800_000_000,
            message: 'The command did not return a terminal receipt.',
            why: 'Inspect Clerk evidence before issuing another lifecycle command.',
          },
        },
      }),
    );
    const { fixture } = await render(BotPanelShellComponent, {
      inputs: { clerkId: 'clrk_spec', broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
      providers: [provideRouter([]), { provide: BrokerV2PanelService, useValue: mockService }, { provide: BrokersService, useValue: brokersMock },
        { provide: MessageService, useValue: messageService }],
    });
    await fixture.whenStable();
    fixture.detectChanges();

    fireEvent.click(screen.getByRole('button', { name: 'Check against Alpaca' }));
    await fixture.whenStable();
    fixture.detectChanges();

    expect(
      screen.getByText('The command did not return a terminal receipt.'),
    ).toBeTruthy();
    expect(
      screen.getByText(
        'Inspect Clerk evidence before issuing another lifecycle command.',
      ),
    ).toBeTruthy();
    expect(messageService.add).toHaveBeenCalledWith(
      expect.objectContaining({
        severity: 'error',
        detail:
          'The command did not return a terminal receipt. Inspect Clerk evidence before issuing another lifecycle command.',
      }),
    );
    // A rejected action refreshes the panel so a superseded "Ready to stop"
    // state doesn't linger (the 2026-08-04 val-nvda-0804-05 409).
    expect(mockService.getLiveSnapshot).toHaveBeenCalledTimes(2);
  });

  it('shows the lane’s own refusal of Reconcile now, sends it once, and re-reads the page (#2874)', async () => {
    const runBotAction = vi.fn().mockRejectedValue(new HttpErrorResponse({
      status: 409,
      error: {
        detail: {
          action_id: 'reconcile_now', outcome: 'conflict', receipt_id: null, recorded_at_ms: 1_753_800_000_000,
          message: 'Custody changed after this action was presented.', why: 'Refresh, then reconcile again.',
          reason_code: null,
        },
      },
    }));
    const { fixture, service } = await renderShell({ runBotAction });

    await userEvent.click(screen.getByRole('button', { name: 'Check against Alpaca' }));
    await fixture.whenStable();
    fixture.detectChanges();

    const outcome = screen.getByRole('alert', { name: 'Action outcome' }).textContent ?? '';
    expect(outcome).toContain('Conflict');
    expect(outcome).toContain('Custody changed after this action was presented.');
    expect(outcome).toContain('Refresh, then reconcile again.');
    expect(runBotAction).toHaveBeenCalledTimes(1);
    // The re-read is what gives the next click the token the Clerk presents now.
    expect(service.getLiveSnapshot).toHaveBeenCalledTimes(2);
  });

  it('renders a cleanup-proven activation failure as Failure, distinct from Unknown', async () => {
    // PRD #1716 FR-6: outcome=failure (cleanup proven) must read differently
    // from outcome=unknown (unproven cleanup) — both the headline label and
    // the backend-authored prose distinguish failed-but-safe from unresolved.
    mockService.getLiveSnapshot.mockResolvedValueOnce(liveSnapshot({
      ...PANEL,
      health: { ...PANEL.health, running: false },
      actions: [RECONCILE_ACTION],
      primary_action: 'reconcile_now',
    }));
    mockService.runBotAction.mockRejectedValueOnce(
      new HttpErrorResponse({
        status: 500,
        error: {
          detail: {
            action_id: 'reconcile_now',
            outcome: 'failure',
            receipt_id: null,
            recorded_at_ms: 1_753_800_000_000,
            message: "Activation failed after Clerk registration for run 'run-2'; the Clerk stop committed.",
            why: null,
            reason_code: null,
          },
        },
      }),
    );
    const { fixture } = await render(BotPanelShellComponent, {
      inputs: { clerkId: 'clrk_spec', broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
      providers: [provideRouter([]), { provide: BrokerV2PanelService, useValue: mockService }, { provide: BrokersService, useValue: brokersMock },
        { provide: MessageService, useValue: messageService }],
    });
    await fixture.whenStable();
    fixture.detectChanges();

    fireEvent.click(screen.getByRole('button', { name: 'Check against Alpaca' }));
    await fixture.whenStable();
    fixture.detectChanges();

    expect(screen.getByText('Failure')).toBeTruthy();
    expect(screen.queryByText('Unknown')).toBeNull();
    expect(
      screen.getByText(
        "Activation failed after Clerk registration for run 'run-2'; the Clerk stop committed.",
      ),
    ).toBeTruthy();
  });

  /**
   * `FleetDirectoryService.refresh()` had no caller before this fix, which is
   * the only reason the pre-freeze click-time fence read was harmless. Now
   * that the fence is frozen at open, a stale-generation refusal must refresh
   * the directory so the operator's next action is minted against a lane
   * they have actually been shown (#2068).
   */
  it('refreshes the directory after the coordinator refuses a stale generation', async () => {
    const directory = provideFleetDirectory({
      observed_at_ms: 1_757_000_000_000,
      clerks: [testLane({ clerk_id: 'clrk_spec' })],
    });
    const refresh = vi.spyOn(directory.useValue as never, 'refresh');
    const runBotAction = vi.fn().mockRejectedValue(
      new HttpErrorResponse({
        status: 409,
        error: { reason: 'clerk_binding_generation_conflict', message: 'Expected 3 is not 4.' },
      }),
    );
    const { fixture } = await renderShell({ directory, runBotAction });

    await userEvent.click(screen.getByRole('button', { name: 'Check against Alpaca' }));
    await fixture.whenStable();

    expect(refresh).toHaveBeenCalledTimes(1);
  });

  /**
   * `refresh()` is a proven-rejecting call (`FleetDirectoryService.refresh` ->
   * `awaitLoaded` throws whenever `/api/broker-clerks` fails). Firing it
   * fire-and-forget with no rejection handler would leave the operator with
   * no signal that the mitigation for a stale-generation refusal didn't
   * take — the directory's `response()` signal stays exactly as stale as it
   * was, and the next action hits the identical refusal with no warning.
   */
  it('tells the operator when the mitigating directory refresh itself fails', async () => {
    const directory = provideFleetDirectory({
      observed_at_ms: 1_757_000_000_000,
      clerks: [testLane({ clerk_id: 'clrk_spec' })],
    });
    directory.useValue.refresh = vi.fn().mockRejectedValue(new Error('directory reload failed'));
    const runBotAction = vi.fn().mockRejectedValue(
      new HttpErrorResponse({
        status: 409,
        error: { reason: 'clerk_binding_generation_conflict', message: 'Expected 3 is not 4.' },
      }),
    );
    const { fixture } = await renderShell({ directory, runBotAction });

    await userEvent.click(screen.getByRole('button', { name: 'Check against Alpaca' }));
    await fixture.whenStable();

    expect(messageService.add).toHaveBeenCalledWith(
      expect.objectContaining({ severity: 'error', detail: LANE_FENCE_REFRESH_FAILED_MESSAGE }),
    );
    expect(runBotAction).toHaveBeenCalledTimes(1);
  });

  it('refuses a panel action whose lane rebound while the action was open', async () => {
    const directory = provideFleetDirectory({
      observed_at_ms: 1_757_000_000_000,
      clerks: [testLane({ clerk_id: 'clrk_spec' })],
    });
    const runBotAction = vi.fn().mockResolvedValue(fakeActionResult());
    const { fixture } = await renderShell({ directory, runBotAction });

    // The operator is shown generation 3, then the coordinator rebinds to 4
    // before they press the button — exactly what refresh() will start doing.
    // The live store's stream address is unfenced (a read, not a command) and
    // restarts on this rebind; let that settle before re-querying the button
    // so the click lands on the current DOM node, not one mid-teardown.
    directory.rebind({
      observed_at_ms: 1_757_000_000_001,
      clerks: [testLane({ clerk_id: 'clrk_spec', effective_binding_generation: 4 })],
    });
    // `rebind()` only stages the replacement; `refresh()` promotes it to
    // what `lanesOf()`/`lane()` report, like the real service's next load.
    await directory.useValue.refresh?.();
    await fixture.whenStable();
    fixture.detectChanges();

    await userEvent.click(screen.getByRole('button', { name: 'Check against Alpaca' }));

    expect(runBotAction).not.toHaveBeenCalled();
    expect(await screen.findByText(/rebound while the action was open/i)).toBeTruthy();
  });

  it('refuses a panel action when the lane had no known binding at open, and dispatches nothing', async () => {
    // Cold directory: the lane is present but its binding is unconfirmed, so
    // `openFence` freezes `{bindingGeneration: null, ...}` (#2068, decision
    // 15). A present-but-null lane, not an absent one: an absent lane would
    // also read as "drifted" by laneFenceDrifted, which would mask a deleted
    // enforceability branch behind the drift branch instead of proving it.
    const directory = provideFleetDirectory({
      observed_at_ms: 1_757_000_000_000,
      clerks: [testLane({ clerk_id: 'clrk_spec', effective_binding_generation: null })],
    });
    const runBotAction = vi.fn().mockResolvedValue(fakeActionResult());
    await renderShell({ directory, runBotAction });

    await userEvent.click(screen.getByRole('button', { name: 'Check against Alpaca' }));

    expect(runBotAction).not.toHaveBeenCalled();
    expect(await screen.findByText(/no known binding when the action was opened/i)).toBeTruthy();
  });

  it('sends the generation the operator was shown, not the one current at click', async () => {
    const directory = provideFleetDirectory({
      observed_at_ms: 1_757_000_000_000,
      clerks: [testLane({ clerk_id: 'clrk_spec' })],
    });
    const runBotAction = vi.fn().mockResolvedValue(fakeActionResult());
    await renderShell({ directory, runBotAction });

    await userEvent.click(screen.getByRole('button', { name: 'Check against Alpaca' }));

    expect(runBotAction).toHaveBeenCalledWith(
      expect.objectContaining({ bindingGeneration: 3, routingEpoch: 4 }),
      expect.anything(), expect.anything(), null,
    );
  });

  describe('the bot’s end (#2607)', () => {
    const END: BotEndView = {
      end_at_ms: Date.UTC(2026, 8, 30, 19, 59),
      end_action: 'SELL',
      status: 'scheduled',
      headline: 'Ends Wed Sep 30, 15:59 ET · sells',
      explanation: 'At Wed Sep 30, 15:59 ET the Clerk stops the bot, cancels its working orders and sells its shares at market.',
      notice: null,
      editable: true,
      default_end_at_ms: null,
    };

    function changeEnd(): HTMLButtonElement {
      return screen.getByRole<HTMLButtonElement>('button', { name: 'Change end' });
    }

    async function saveNoEnd(): Promise<HTMLElement> {
      await userEvent.click(changeEnd());
      const editor = screen.getByRole('dialog', { name: 'Change this bot’s end' });
      await userEvent.click(within(editor).getByRole('checkbox', { name: 'No end — run until I stop it' }));
      await userEvent.click(within(editor).getByRole('button', { name: 'Save end' }));
      return editor;
    }

    it('changes the end as a command on the lane the owner was shown, then re-reads the panel', async () => {
      const editBotEnd = vi.fn().mockResolvedValue({ ...END, end_at_ms: null, status: 'no_end' });
      const { service } = await renderShell({ end: END, editBotEnd });
      const reads = service.getLiveSnapshot.mock.calls.length;

      await saveNoEnd();

      await vi.waitFor(() => expect(editBotEnd).toHaveBeenCalledWith(
        expect.objectContaining({ bindingGeneration: 3, routingEpoch: 4, idempotencyKey: expect.any(String) }),
        'sid-001',
        { end_at_ms: null, end_action: 'SELL' },
      ));
      await vi.waitFor(() => expect(service.getLiveSnapshot.mock.calls.length).toBeGreaterThan(reads));
    });

    it('holds Change still while a command is on its way, so a change of end never races it', async () => {
      const pending = deferred<PanelActionResult>();
      const { fixture } = await renderShell({ end: END, runBotAction: vi.fn().mockReturnValueOnce(pending.promise) });

      fireEvent.click(screen.getByRole('button', { name: 'Check against Alpaca' }));
      await fixture.whenStable();
      fixture.detectChanges();

      expect(changeEnd().disabled).toBe(true);
      pending.resolve(fakeActionResult());
      await vi.waitFor(() => expect(changeEnd().disabled).toBe(false));
    });

    it('sends no command while a change of the end is on its way', async () => {
      const pending = deferred<BotEndView>();
      const runBotAction = vi.fn().mockResolvedValue(fakeActionResult());
      const { fixture } = await renderShell({ end: END, runBotAction, editBotEnd: vi.fn().mockReturnValueOnce(pending.promise) });

      await saveNoEnd();
      await fixture.whenStable();
      fireEvent.click(screen.getByRole('button', { name: 'Check against Alpaca' }));
      await fixture.whenStable();

      expect(runBotAction).not.toHaveBeenCalled();
      pending.resolve({ ...END, end_at_ms: null, status: 'no_end' });
    });

    it('refreshes the lane and the panel after a stale-generation refusal of the end, and shows it', async () => {
      const directory = provideFleetDirectory({
        observed_at_ms: 1_757_000_000_000,
        clerks: [testLane({ clerk_id: 'clrk_spec' })],
      });
      const refresh = vi.spyOn(directory.useValue as never, 'refresh');
      const editBotEnd = vi.fn().mockRejectedValue(new HttpErrorResponse({
        status: 409,
        error: { reason: 'clerk_binding_generation_conflict', message: 'Expected 3 is not 4.', next_step: 'Reload the lane.' },
      }));
      const { service } = await renderShell({ directory, end: END, editBotEnd });
      const reads = service.getLiveSnapshot.mock.calls.length;

      const editor = await saveNoEnd();

      expect((await within(editor).findByRole('alert')).textContent).toContain('Expected 3 is not 4.');
      expect(refresh).toHaveBeenCalledTimes(1);
      expect(service.getLiveSnapshot.mock.calls.length).toBeGreaterThan(reads);
      expect(changeEnd().disabled).toBe(false);
    });
  });

  describe('the one view (#2563)', () => {
    beforeEach(() => {
      brokersMock.checkSqliteRecoveryActionInFull.mockReset();
      brokersMock.checkSqliteRecoveryActionInFull.mockResolvedValue({
        capability: SAFE_FLATTEN_CAPABILITY,
        reduction_pricing: { kind: 'regular_session' },
      });
    });

    async function renderPage(panel: BotPanelView, service: Partial<typeof mockService> = {}) {
      const view = await render(BotPanelShellComponent, {
        inputs: { clerkId: 'clrk_spec', broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
        providers: [
          provideRouter([]),
          {
            provide: BrokerV2PanelService,
            useValue: { ...mockService, getLiveSnapshot: vi.fn().mockResolvedValue(liveSnapshot(panel)), ...service },
          },
          { provide: BrokersService, useValue: brokersMock },
          { provide: MessageService, useValue: messageService },
        ],
      });
      await view.fixture.whenStable();
      view.fixture.detectChanges();
      return view;
    }

    function statement(): string[] {
      const list = screen.getByRole('region', { name: "This bot's money" }).querySelector('dl');
      return [...(list?.querySelectorAll('dt') ?? [])].map((term) =>
        `${term.textContent?.trim()} ${term.nextElementSibling?.textContent?.trim()}`);
    }

    it('leads with the bot’s own status and the backend’s summary, and no LIVE chip (#2794 R1, R3)', async () => {
      const { container } = await renderPage({ ...PANEL, mode: 'trade' });

      const banner = container.querySelector('app-bot-page-header');
      expect(banner?.textContent).toContain('Running');
      await userEvent.click(screen.getByRole('button', { name: 'Running' }));
      expect(screen.getByText('Running since Tue Nov 14 2023, 17:13 ET · no decisions, no trades.')).toBeTruthy();
      expect(banner?.textContent).not.toContain('LIVE');
      expect(banner?.querySelector('app-alpaca-lane-mode-chip')).toBeNull();
    });

    it('splits health into this run and the account right now (#2794 R9)', async () => {
      await renderPage({ ...PANEL, mode: 'trade' });

      const health = screen.getByRole('region', { name: 'Health' });
      expect(within(health).getAllByRole('heading', { level: 4 }).map((heading) => heading.textContent?.trim()))
        .toEqual(['During this run', 'Account right now', 'Market right now']);
      expect(within(health).getByRole('list', { name: 'During this run' }).textContent).toContain('IBKR market data feed');
    });

    it('reads a finished run’s tape from that run’s window, not the newest bars (#2794 R7)', async () => {
      const getHistoryChart = vi.fn().mockResolvedValue(mockService.getHistoryChart());
      await renderPage({ ...PANEL, mode: 'trade', health: { ...PANEL.health, running: false } }, { getHistoryChart });

      // The fixture's run ran a minute to 1_700_000_001_000; the tape reaches 15 minutes either side.
      await vi.waitFor(() => expect(getHistoryChart).toHaveBeenLastCalledWith(
        expect.anything(), 'sid-001', '1m', { fromMs: 1_700_000_001_000 - 60_000 - 900_000, toMs: 1_700_000_001_000 + 900_000 },
      ));
    });

    it('says no bot by that name is on this account, with the way back, instead of loading forever', async () => {
      const missing = new HttpErrorResponse({
        status: 404,
        error: { detail: { message: 'No bot \'sid-001\' is bound to broker \'alpaca\'.', why: null, next_action: null } },
      });
      await renderPage(PANEL, { getLiveSnapshot: vi.fn().mockRejectedValue(missing) });

      const notFound = await screen.findByRole('alert', { name: 'No bot named sid-001 on this account' });
      expect(within(notFound).getByText('No bot \'sid-001\' is bound to broker \'alpaca\'.')).toBeTruthy();
      expect(within(notFound).getByRole('link', { name: 'Back to Home' })).toBeTruthy();
      expect(screen.queryByText('Loading bot control…')).toBeNull();
    });

    it('shows a running bot with Stop, its money statement verbatim, and no stranded warning', async () => {
      await renderPage({
        ...PANEL, mode: 'trade', exposure: { QQQ: 2.5 }, actions: [fakeSqliteStopAction()], primary_action: 'stop_bot_decisions',
        open_pnl: -3.254, open_pnl_usd: '-3.25', open_pnl_direction: 'loss',
      });
      await screen.findByText('Holding its position');

      expect(screen.getByRole('button', { name: 'Stop' })).toBeTruthy();
      expect(statement()).toEqual([
        'Budget set aside at deploy $1,000.00',
        'Realized gains and losses $0.00',
        'Fees -$0.01',
        'Balance $999.99',
        'In shares, at cost $764.71',
        'Waiting in entry orders $0.00',
        'Free to trade $235.28',
      ]);
      // The panel's own authored dollars, never a browser-formatted number.
      expect(screen.getByText(/Open gain or loss on shares:/).textContent?.replace(/\s+/g, ' ').trim())
        .toBe('Open gain or loss on shares: -$3.25, counted when sold.');
      expect(screen.queryByRole('heading', { name: /No bot is managing/ })).toBeNull();
      expect(screen.queryByRole('link', { name: 'Deploy again' })).toBeNull();
    });

    it('warns about a stopped bot still holding shares, with Flatten and Deploy again beside it', async () => {
      await renderPage(strandedPanel(), { getBudget: vi.fn().mockResolvedValue(STOPPED_BUDGET) });
      await screen.findByText('Stopped · still holds shares');

      const warning = screen.getByRole('region', { name: /No bot is managing 2.5 QQQ/ });
      expect(within(warning).getByText('Crashed: market data stopped')).toBeTruthy();
      expect(within(warning).getByRole('button', { name: 'Flatten…' })).toBeTruthy();
      expect(within(warning).getByText(
        'Deploy again starts a new bot with its own budget. It never takes over these shares.',
      )).toBeTruthy();
      expect(screen.getByRole('link', { name: 'Deploy again' }).getAttribute('href'))
        .toBe('/brokers/alpaca/clerks/clrk_spec/accounts/DUM284968/deploy?from=sid-001');
      expect(statement().slice(3)).toEqual([
        'Balance $999.99',
        'Released at stop $235.28',
        'Still in shares, at cost $764.71',
        'Still in entry orders $0.00',
      ]);
    });

    it('moves the keyboard to the outcome after a command (story 48)', async () => {
      await renderPage({ ...PANEL, mode: 'trade', actions: [RECONCILE_ACTION], primary_action: 'reconcile_now' });

      await userEvent.click(screen.getByRole('button', { name: 'Check against Alpaca' }));

      await vi.waitFor(() => expect(document.activeElement?.classList.contains('action-receipt')).toBe(true));
      expect(document.activeElement?.textContent).toContain('Reconciliation requested.');
    });

    it('flattens on one confirmation: reconcile, fresh panel, checked plan, then send (H30)', async () => {
      const getPanel = vi.fn()
        .mockResolvedValueOnce(strandedPanel())
        .mockResolvedValueOnce(strandedPanel({ actions: [RECONCILE_ACTION, PREPARE_SAFE_FLATTEN_ACTION] }))
        .mockResolvedValueOnce(strandedPanel({ actions: [RECONCILE_ACTION, ENABLED_EXECUTE] }));
      const runBotAction = vi.fn()
        .mockResolvedValueOnce(fakeActionResult({ action_id: 'reconcile_now', message: 'Reconciled with Alpaca.' }))
        .mockResolvedValueOnce(fakeActionResult({ action_id: 'execute_safe_flatten', message: 'Sale of 2.5 QQQ sent.' }));
      await renderPage(strandedPanel(), { getPanel, runBotAction, getBudget: vi.fn().mockResolvedValue(STOPPED_BUDGET) });

      await userEvent.click(screen.getByRole('button', { name: 'Flatten…' }));
      const confirm = screen.getByRole('group', { name: 'Sell 2.5 QQQ?' });
      await userEvent.click(within(confirm).getByRole('button', { name: 'Sell 2.5 QQQ' }));

      await vi.waitFor(() => expect(runBotAction).toHaveBeenCalledTimes(2));
      expect(runBotAction.mock.calls.map(([, , action]) => action.action_id))
        .toEqual(['reconcile_now', 'execute_safe_flatten']);
      expect(runBotAction.mock.calls[1][2].concurrency_token).toBe('execute-token-18');
      const check = brokersMock.checkSqliteRecoveryActionInFull;
      expect(check).toHaveBeenCalledWith(
        'clrk_spec', 'DUM284968',
        { action_id: 'prepare_safe_flatten', concurrency_token: 'plan-token-17' },
        'sid-001',
      );
      const order = [
        getPanel.mock.invocationCallOrder[0],
        runBotAction.mock.invocationCallOrder[0],
        getPanel.mock.invocationCallOrder[1],
        check.mock.invocationCallOrder[0],
        getPanel.mock.invocationCallOrder[2],
        runBotAction.mock.invocationCallOrder[1],
      ];
      expect(order).toEqual([...order].sort((a, b) => a - b));
      await vi.waitFor(() => expect(document.activeElement?.classList.contains('action-receipt')).toBe(true));
      expect(document.activeElement?.textContent).toContain('Sale of 2.5 QQQ sent.');
      expect(screen.getByRole('list', { name: 'Flatten progress' }).textContent).toContain('Sell: Done');
    });

    it('sends nothing when the prepared sale does not match what the owner confirmed', async () => {
      const getPanel = vi.fn()
        .mockResolvedValueOnce(strandedPanel())
        .mockResolvedValueOnce(strandedPanel({ actions: [RECONCILE_ACTION, PREPARE_SAFE_FLATTEN_ACTION] }));
      const runBotAction = vi.fn().mockResolvedValue(fakeActionResult({ action_id: 'reconcile_now', message: 'Reconciled.' }));
      brokersMock.checkSqliteRecoveryActionInFull.mockResolvedValueOnce({
        capability: {
          ...SAFE_FLATTEN_CAPABILITY,
          reduction_plan: { ...SAFE_FLATTEN_PLAN, legs: [{ ...SAFE_FLATTEN_PLAN.legs[0], quantity: 3 }] },
        },
        reduction_pricing: { kind: 'regular_session' },
      });
      await renderPage(strandedPanel(), { getPanel, runBotAction });

      await userEvent.click(screen.getByRole('button', { name: 'Flatten…' }));
      await userEvent.click(within(screen.getByRole('group', { name: 'Sell 2.5 QQQ?' })).getByRole('button', { name: 'Sell 2.5 QQQ' }));

      await vi.waitFor(() => expect(document.activeElement?.classList.contains('action-receipt')).toBe(true));
      expect(runBotAction).toHaveBeenCalledTimes(1);
      expect(document.activeElement?.textContent).toContain('does not match the 2.5 QQQ you confirmed. Nothing was sent.');
      expect(screen.getByRole('list', { name: 'Flatten progress' }).textContent).toContain('Prepare the sale: Failed');
    });

    it('stops at the checked plan outside regular hours and hands the keyboard to the limit ticket', async () => {
      const getPanel = vi.fn()
        .mockResolvedValueOnce(strandedPanel())
        .mockResolvedValueOnce(strandedPanel({ actions: [RECONCILE_ACTION, PREPARE_SAFE_FLATTEN_ACTION] }));
      const runBotAction = vi.fn().mockResolvedValue(fakeActionResult({ action_id: 'reconcile_now', message: 'Reconciled.' }));
      brokersMock.checkSqliteRecoveryActionInFull.mockResolvedValue({
        capability: SAFE_FLATTEN_CAPABILITY,
        reduction_pricing: {
          kind: 'extended_limit', phase: 'PRE', symbol: 'QQQ', side: 'sell',
          bid: 480.1, ask: 480.2, bid_size: 300, ask_size: 200,
          quote_observed_at_ms: Date.now(), quote_max_age_ms: 10_000,
          exit_allowance_bps: 20, suggested_limit_price: 479.13, band_limit_price: 478.17,
          spread: 0.1, spread_bps: 2.08, wide_spread: false, spread_warning_bps: 50, proposal: null,
        },
      });
      await renderPage(strandedPanel(), { getPanel, runBotAction });

      await userEvent.click(screen.getByRole('button', { name: 'Flatten…' }));
      await userEvent.click(within(screen.getByRole('group', { name: 'Sell 2.5 QQQ?' })).getByRole('button', { name: 'Sell 2.5 QQQ' }));

      await vi.waitFor(() => expect(document.activeElement?.classList.contains('flatten-ticket')).toBe(true));
      expect(runBotAction).toHaveBeenCalledTimes(1);
      expect(screen.getByRole('list', { name: 'Flatten progress' }).textContent)
        .toContain('Outside regular hours this sale needs a limit price. Set it below.');
      expect(screen.getByRole('button', { name: 'Flatten…' }).hasAttribute('disabled')).toBe(true);
    });

    it('checks and sells a Dry Run’s shares in its simulated account, never at Alpaca', async () => {
      const dryRun = (overrides: Partial<BotPanelView> = {}) => strandedPanel({ mode: 'dry_run', ...overrides });
      const getPanel = vi.fn()
        .mockResolvedValueOnce(dryRun())
        .mockResolvedValueOnce(dryRun({ actions: [RECONCILE_ACTION, PREPARE_SAFE_FLATTEN_ACTION] }))
        .mockResolvedValueOnce(dryRun({ actions: [RECONCILE_ACTION, ENABLED_EXECUTE] }));
      const runBotAction = vi.fn().mockResolvedValue(fakeActionResult({ message: 'Done.' }));
      await renderPage(dryRun(), { getPanel, runBotAction });

      await userEvent.click(screen.getByRole('button', { name: 'Flatten…' }));
      const confirm = screen.getByRole('group', { name: 'Sell 2.5 QQQ?' });
      expect(confirm.textContent).toContain('The sale is simulated at the live IBKR price; nothing is sent to Alpaca.');
      expect(confirm.textContent).not.toContain('checked with Alpaca');
      expect(confirm.textContent).not.toContain('free to deploy');
      await userEvent.click(within(confirm).getByRole('button', { name: 'Sell 2.5 QQQ' }));

      await vi.waitFor(() => expect(runBotAction).toHaveBeenCalledTimes(2));
      const steps = screen.getByRole('list', { name: 'Flatten progress' }).textContent ?? '';
      expect(steps).toContain('Check the position in its simulated account: Done');
      expect(steps).not.toContain('Alpaca');
    });

    it('returns the keyboard to Flatten when the confirmation is cancelled', async () => {
      await renderPage(strandedPanel());

      await userEvent.click(screen.getByRole('button', { name: 'Flatten…' }));
      await vi.waitFor(() => expect(document.activeElement?.textContent?.trim()).toBe('Sell 2.5 QQQ'));
      await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));

      await vi.waitFor(() => expect(document.activeElement?.textContent?.trim()).toBe('Flatten…'));
      expect(brokersMock.checkSqliteRecoveryActionInFull).not.toHaveBeenCalled();
    });

    it.each([
      ['running', { ...PANEL, mode: 'trade' as const, actions: [fakeSqliteStopAction()], primary_action: 'stop_bot_decisions' as const }],
      ['stopped and holding', strandedPanel()],
    ])('has no detectable accessibility violations (%s)', async (_name, panel) => {
      await renderPage(panel);
      await screen.findByText('Holding its position');

      const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });
      expect(results.violations).toEqual([]);
    });
  });

  describe('settling an uncertain closing order (#2854)', () => {
    /** A stopped bot still holding 2.5 QQQ whose closing order the Clerk could not confirm. */
    const SETTLE_ACTION = fakeSqliteSettleAction({ revision: 17, concurrency_token: 'settle-token-17' });
    const NOTHING_AT_ALPACA: SqliteAttestationFindings = {
      attestation: 'absent',
      would_settle: true,
      refusal_reason_code: null,
      lines: ['Alpaca shows no order under this order\'s id.', 'No order is working on QQQ.'],
      observed_at_ms: 1_753_800_000_500,
    };
    const CHECKED = {
      capability: { ...SAFE_FLATTEN_CAPABILITY, action_id: 'settle_unknown_exit_order', reduction_plan: null },
      attestation_findings: NOTHING_AT_ALPACA,
    } satisfies SqliteRecoveryActionCheck;
    const SETTLED = fakeActionResult({
      action_id: 'settle_unknown_exit_order', receipt_id: 'transition:41',
      concurrency_token: 'settle-token-17', message: 'Settle the uncertain order completed.',
    });
    const BUSY_MESSAGE = 'The Clerk is working on this exit right now. Try again in a few seconds. Nothing was changed.';
    const NO_ANSWER_MESSAGE = 'No answer came back, so the result is not known.';
    const NO_ANSWER = new HttpErrorResponse({ status: 0, statusText: 'Unknown Error' });
    const REFUSED_BY_THE_CLERK = new HttpErrorResponse({
      status: 409,
      error: {
        detail: {
          action_id: 'settle_unknown_exit_order', outcome: 'failure', receipt_id: null, recorded_at_ms: 1,
          message: BUSY_MESSAGE, why: null, reason_code: null,
        },
      },
    });

    /** The page as a later read of the panel shows it: with the settle action as given, or none. */
    function pageSnapshot(version: number, settle: PanelAction | null = SETTLE_ACTION): BotPanelLiveSnapshot {
      return {
        ...liveSnapshot(strandedPanel({ actions: settle === null ? [RECONCILE_ACTION] : [RECONCILE_ACTION, settle] })),
        surface_version: version,
      };
    }

    async function renderSettle(
      runBotAction = vi.fn().mockResolvedValue(SETTLED),
      getLiveSnapshot = vi.fn().mockResolvedValue(pageSnapshot(1)),
    ) {
      const checkSettlement = vi.fn().mockResolvedValue(CHECKED);
      const view = await render(BotPanelShellComponent, {
        inputs: { clerkId: 'clrk_spec', broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
        providers: [
          provideRouter([]),
          { provide: BrokerV2PanelService, useValue: { ...mockService, getLiveSnapshot, runBotAction } },
          { provide: BrokersService, useValue: { ...brokersMock, checkSqliteRecoveryActionInFull: checkSettlement } },
          { provide: MessageService, useValue: messageService },
        ],
      });
      await view.fixture.whenStable();
      view.fixture.detectChanges();
      return { ...view, runBotAction, checkSettlement, getLiveSnapshot, user: userEvent.setup() };
    }

    type User = ReturnType<typeof userEvent.setup>;

    function openedTicket(): HTMLElement | null {
      return screen.queryByRole('region', { name: 'Settle the uncertain order' });
    }

    async function openTicket(user: User): Promise<HTMLElement> {
      const toolbar = screen.getByRole('toolbar', { name: 'Actions for this bot' });
      await user.click(within(toolbar).getByRole('button', { name: 'Settle the uncertain order' }));
      return screen.findByRole('region', { name: 'Settle the uncertain order' });
    }

    /** Say "Alpaca has no such order" and have the Clerk check it. */
    async function sayNoSuchOrder(user: User, ticket: HTMLElement): Promise<void> {
      await user.click(within(ticket).getByRole('radio', { name: /^Alpaca has no such order/ }));
      await user.click(within(ticket).getByRole('button', { name: 'Ask the Clerk to check' }));
      await within(ticket).findByRole('status', { name: 'What the Clerk found' });
    }

    async function confirmSettle(user: User, ticket: HTMLElement): Promise<void> {
      await user.click(within(ticket).getByRole('button', { name: 'Settle the order…' }));
      const dialog = screen.getByRole('dialog', { name: 'Settle the uncertain order?' });
      await user.type(within(dialog).getByRole('textbox'), 'SETTLE');
      await user.click(within(dialog).getByRole('button', { name: 'Settle the order' }));
    }

    /** A settlement on its way, whose answer the test gives when it is ready. */
    function settlementInFlight() {
      let refuse: (error: unknown) => void = () => undefined;
      const runBotAction = vi.fn().mockReturnValueOnce(new Promise<PanelActionResult>((_resolve, reject) => {
        refuse = reject;
      }));
      return { runBotAction, refuse: (error: unknown) => refuse(error) };
    }

    const keyOfTry = (runBotAction: ReturnType<typeof vi.fn>, index: number): unknown =>
      runBotAction.mock.calls[index][0].idempotencyKey;

    it('opens the ticket from the toolbar with the Clerk’s words, and sends and confirms nothing yet', async () => {
      const { runBotAction, checkSettlement, user } = await renderSettle();

      const ticket = await openTicket(user);

      expect(ticket.textContent).toContain(SETTLE_ACTION.settlement?.ticket);
      // The ticket holds the confirmation: the button's own plain "are you sure" never opens.
      expect(screen.queryByRole('dialog', { name: 'Settle the uncertain order?' })).toBeNull();
      expect(runBotAction).not.toHaveBeenCalled();
      expect(checkSettlement).not.toHaveBeenCalled();
      await vi.waitFor(() => expect(document.activeElement).toBe(ticket));
    });

    it('has the Clerk check on the presented token, then settles with what was said and shows the receipt', async () => {
      const { runBotAction, checkSettlement, user } = await renderSettle();
      const ticket = await openTicket(user);

      await sayNoSuchOrder(user, ticket);

      expect(checkSettlement).toHaveBeenCalledExactlyOnceWith(
        'clrk_spec', 'DUM284968',
        { action_id: 'settle_unknown_exit_order', concurrency_token: 'settle-token-17', attestation: { kind: 'absent' } },
        'sid-001',
      );
      expect(runBotAction).not.toHaveBeenCalled();

      await confirmSettle(user, ticket);

      await vi.waitFor(() => expect(runBotAction).toHaveBeenCalledTimes(1));
      const [target, sid, action, reason, attestation] = runBotAction.mock.calls[0];
      expect(target).toMatchObject({
        broker: 'alpaca', clerkId: 'clrk_spec', accountId: 'DUM284968', entityId: 'sid-001', capability: 'bot_action',
      });
      expect(target.idempotencyKey).toEqual(expect.any(String));
      expect([sid, action, reason, attestation]).toEqual(['sid-001', SETTLE_ACTION, null, { kind: 'absent' }]);
      // The result is the page's receipt, in the backend's words; the ticket is done.
      expect(await screen.findByText('Settle the uncertain order completed.')).toBeTruthy();
      expect(openedTicket()).toBeNull();
    });

    it('says the result is not known when no answer came back, re-reads the panel, and sends the next try under a new key', async () => {
      const runBotAction = vi.fn().mockRejectedValueOnce(NO_ANSWER).mockResolvedValueOnce(SETTLED);
      const { getLiveSnapshot, user } = await renderSettle(runBotAction);
      const ticket = await openTicket(user);
      await sayNoSuchOrder(user, ticket);
      const readsBefore = getLiveSnapshot.mock.calls.length;

      await confirmSettle(user, ticket);

      // The panel still offers this order, so the ticket stays and says what is known: nothing.
      expect((await within(ticket).findByRole('alert')).textContent).toContain(NO_ANSWER_MESSAGE);
      expect(getLiveSnapshot.mock.calls.length).toBeGreaterThan(readsBefore);
      expect(screen.queryByText('Settle the uncertain order completed.')).toBeNull();

      // The fleet never delivers a key twice: the next try is a command of its own.
      await confirmSettle(user, ticket);
      expect(await screen.findByText('Settle the uncertain order completed.')).toBeTruthy();
      expect(runBotAction).toHaveBeenCalledTimes(2);
      expect(keyOfTry(runBotAction, 1)).not.toBe(keyOfTry(runBotAction, 0));
      for (const call of runBotAction.mock.calls) expect(call[4]).toEqual({ kind: 'absent' });
    });

    it.each([
      [503, {
        reason: 'clerk_routing_outcome_unknown',
        message: 'Clerk clrk_spec failed serving bot_panel_quiesce_action with 500 after the dispatch.',
        next_step: 'The command may have been applied. Reconcile by the idempotency key with the provider clerk\'s receipt.',
      }],
      [409, {
        reason: 'broker_clerk_capability_unavailable',
        message: 'Clerk clrk_spec does not serve bot actions right now.',
        next_step: 'Wait for the lane to become ready, then try again.',
      }],
    ])('shows the fleet’s own refusal (HTTP %i), keeps the ticket usable, and sends the next try under a new key', async (status, body) => {
      // The fleet's refusal is flat and names no outcome.
      const runBotAction = vi.fn()
        .mockRejectedValueOnce(new HttpErrorResponse({ status, error: body }))
        .mockResolvedValueOnce(SETTLED);
      const { user } = await renderSettle(runBotAction);
      const ticket = await openTicket(user);
      await sayNoSuchOrder(user, ticket);

      await confirmSettle(user, ticket);

      const refused = await within(ticket).findByRole('alert');
      expect(refused.textContent).toContain(body.message);
      expect(refused.textContent).toContain(body.next_step);
      // What the Clerk found is still there, and so is the way to settle.
      expect(within(ticket).getByRole('status', { name: 'What the Clerk found' })).toBeTruthy();

      await confirmSettle(user, ticket);
      expect(await screen.findByText('Settle the uncertain order completed.')).toBeTruthy();
      expect(runBotAction).toHaveBeenCalledTimes(2);
      expect(keyOfTry(runBotAction, 1)).not.toBe(keyOfTry(runBotAction, 0));
    });

    it('closes the ticket and says so on the page when, after no answer, the re-read panel no longer offers the order', async () => {
      // The order was settled and only the answer was lost: the next read of the panel shows it.
      const getLiveSnapshot = vi.fn()
        .mockResolvedValueOnce(pageSnapshot(1))
        .mockResolvedValue(pageSnapshot(2, null));
      const runBotAction = vi.fn().mockRejectedValueOnce(NO_ANSWER);
      const { user } = await renderSettle(runBotAction, getLiveSnapshot);
      const ticket = await openTicket(user);
      await sayNoSuchOrder(user, ticket);

      await confirmSettle(user, ticket);

      // The ticket is gone with its order, so the page says what happened to the command.
      const receipt = await screen.findByText(NO_ANSWER_MESSAGE);
      expect(openedTicket()).toBeNull();
      expect(ticket.contains(receipt)).toBe(false);
      expect(within(screen.getByRole('toolbar', { name: 'Actions for this bot' }))
        .queryByRole('button', { name: 'Settle the uncertain order' })).toBeNull();
      expect(runBotAction).toHaveBeenCalledTimes(1);
    });

    it('keeps saying the result is not known when a later panel withdraws the order the ticket said it of', async () => {
      const runBotAction = vi.fn().mockRejectedValueOnce(NO_ANSWER);
      const { fixture, user } = await renderSettle(runBotAction);
      const ticket = await openTicket(user);
      await sayNoSuchOrder(user, ticket);
      await confirmSettle(user, ticket);
      // The re-read panel still offers the order, so the ticket is where it is said.
      expect((await within(ticket).findByRole('alert')).textContent).toContain(NO_ANSWER_MESSAGE);
      expect(screen.queryByRole('alert', { name: 'Action outcome' })).toBeNull();

      // A later panel has nothing to settle -- that command may be why -- and the ticket goes with its order.
      emitOnLiveStream('snapshot', JSON.stringify(pageSnapshot(2, null)));
      await fixture.whenStable();

      expect(openedTicket()).toBeNull();
      const receipt = screen.getByRole('alert', { name: 'Action outcome' });
      expect(receipt.textContent).toContain(NO_ANSWER_MESSAGE);
      await vi.waitFor(() => expect(document.activeElement).toBe(receipt));
    });

    it('closes the ticket when the page stops offering the order, and hands its keyboard to the toolbar', async () => {
      const { fixture, runBotAction, user } = await renderSettle();
      const ticket = await openTicket(user);
      await sayNoSuchOrder(user, ticket);
      expect(ticket.contains(document.activeElement)).toBe(true);

      // The Clerk found the order by itself: the next panel has nothing to settle.
      emitOnLiveStream('snapshot', JSON.stringify(pageSnapshot(2, null)));
      await fixture.whenStable();

      expect(openedTicket()).toBeNull();
      expect(runBotAction).not.toHaveBeenCalled();
      // The order's own entry left the toolbar with it; the keyboard is on the toolbar, not the page's body.
      const toolbar = screen.getByRole('toolbar', { name: 'Actions for this bot' });
      await vi.waitFor(() => expect(toolbar.contains(document.activeElement)).toBe(true));
    });

    it('leaves the keyboard alone when the page closes a ticket that did not have it', async () => {
      const { fixture, user } = await renderSettle();
      await openTicket(user);
      const elsewhere = screen.getByRole('button', { name: 'Flatten…' });
      elsewhere.focus();

      emitOnLiveStream('snapshot', JSON.stringify(pageSnapshot(2, null)));
      await fixture.whenStable();

      expect(openedTicket()).toBeNull();
      expect(document.activeElement).toBe(elsewhere);
    });

    it('starts the ticket over on the order’s newer send, and checks that one', async () => {
      const { fixture, checkSettlement, runBotAction, user } = await renderSettle();
      const ticket = await openTicket(user);
      await sayNoSuchOrder(user, ticket);
      expect(ticket.contains(document.activeElement)).toBe(true);

      // The Clerk sent the order again: the page presents the action under a new token.
      const sentAgain = fakeSqliteSettleAction({ revision: 18, concurrency_token: 'settle-token-18' });
      emitOnLiveStream('snapshot', JSON.stringify(pageSnapshot(2, sentAgain)));
      await fixture.whenStable();

      // Still open, with nothing said and nothing found: what was read was read for the older send.
      expect(openedTicket()).toBe(ticket);
      expect(within(ticket).queryByRole('status', { name: 'What the Clerk found' })).toBeNull();
      expect(within(ticket).queryByRole('button', { name: 'Settle the order…' })).toBeNull();
      expect(within(ticket).getAllByRole<HTMLInputElement>('radio').some((radio) => radio.checked)).toBe(false);
      // What had the keyboard is gone from the ticket: it goes to the toolbar's entry for the order.
      const entry = within(screen.getByRole('toolbar', { name: 'Actions for this bot' }))
        .getByRole('button', { name: 'Settle the uncertain order' });
      await vi.waitFor(() => expect(document.activeElement).toBe(entry));

      await sayNoSuchOrder(user, ticket);
      expect(checkSettlement).toHaveBeenLastCalledWith(
        'clrk_spec', 'DUM284968',
        { action_id: 'settle_unknown_exit_order', concurrency_token: 'settle-token-18', attestation: { kind: 'absent' } },
        'sid-001',
      );
      await confirmSettle(user, ticket);
      await vi.waitFor(() => expect(runBotAction).toHaveBeenCalledTimes(1));
      expect(runBotAction.mock.calls[0][2]).toEqual(sentAgain);
    });

    it('re-reads the panel after a refused check, and says the refusal on the page when the ticket moved to a newer send', async () => {
      // The Clerk sent the order again and the page has not heard yet: the check is refused as stale.
      const stale = 'The recovery evidence changed. Refresh before confirming the action.';
      const sentAgain = fakeSqliteSettleAction({ revision: 18, concurrency_token: 'settle-token-18' });
      const getLiveSnapshot = vi.fn()
        .mockResolvedValueOnce(pageSnapshot(1))
        .mockResolvedValue(pageSnapshot(2, sentAgain));
      const { checkSettlement, user } = await renderSettle(undefined, getLiveSnapshot);
      checkSettlement.mockRejectedValueOnce(new HttpErrorResponse({
        status: 409,
        error: { detail: { reason: 'stale_action_token', message: stale } },
      }));
      const ticket = await openTicket(user);
      await user.click(within(ticket).getByRole('radio', { name: /^Alpaca has no such order/ }));

      await user.click(within(ticket).getByRole('button', { name: 'Ask the Clerk to check' }));

      // The refusal was of the older send. The ticket is on the newer one now, with nothing said,
      // so the page says why: its receipt carries the refusal and has the keyboard.
      const receipt = await screen.findByRole('alert', { name: 'Action outcome' });
      expect(receipt.textContent).toContain(stale);
      expect(getLiveSnapshot).toHaveBeenCalledTimes(2);
      expect(openedTicket()).toBe(ticket);
      expect(within(ticket).queryByRole('alert')).toBeNull();
      expect(within(ticket).getAllByRole<HTMLInputElement>('radio').some((radio) => radio.checked)).toBe(false);
      await vi.waitFor(() => expect(document.activeElement).toBe(receipt));

      // The ticket asks about the newer send.
      await sayNoSuchOrder(user, ticket);
      expect(checkSettlement).toHaveBeenLastCalledWith(
        'clrk_spec', 'DUM284968',
        { action_id: 'settle_unknown_exit_order', concurrency_token: 'settle-token-18', attestation: { kind: 'absent' } },
        'sid-001',
      );
    });

    it('keeps the ticket open across a re-read of the panel that presents the same send', async () => {
      const { fixture, user } = await renderSettle();
      const ticket = await openTicket(user);
      await sayNoSuchOrder(user, ticket);

      emitOnLiveStream('snapshot', JSON.stringify(pageSnapshot(2)));
      await fixture.whenStable();

      expect(openedTicket()).toBe(ticket);
      expect(within(ticket).getByRole('status', { name: 'What the Clerk found' })).toBeTruthy();
      expect(within(ticket).getByRole('button', { name: 'Settle the order…' })).toBeTruthy();
    });

    it('names the bot in a notification when a refusal lands after the owner moved to another bot', async () => {
      const { runBotAction, refuse } = settlementInFlight();
      const { fixture, user } = await renderSettle(runBotAction);
      const ticket = await openTicket(user);
      await sayNoSuchOrder(user, ticket);
      await confirmSettle(user, ticket);
      await vi.waitFor(() => expect(runBotAction).toHaveBeenCalledTimes(1));

      fixture.componentRef.setInput('sid', 'sid-002');
      await fixture.whenStable();
      expect(openedTicket()).toBeNull();
      messageService.add.mockClear();
      refuse(REFUSED_BY_THE_CLERK);

      await vi.waitFor(() => expect(messageService.add).toHaveBeenCalledTimes(1));
      const toast = messageService.add.mock.calls[0][0] as { severity: string; detail: string };
      expect(toast.severity).toBe('error');
      expect(toast.detail).toContain('Ema Crossover');
      expect(toast.detail).toContain(BUSY_MESSAGE);
      // Nothing of the old bot's refusal is drawn on the bot now shown.
      expect(screen.queryByText(BUSY_MESSAGE)).toBeNull();
    });

    it('names the bot in a notification when no answer came back and the owner has left the page', async () => {
      const { runBotAction, refuse } = settlementInFlight();
      const { fixture, user } = await renderSettle(runBotAction);
      const ticket = await openTicket(user);
      await sayNoSuchOrder(user, ticket);
      await confirmSettle(user, ticket);
      await vi.waitFor(() => expect(runBotAction).toHaveBeenCalledTimes(1));

      fixture.destroy();
      messageService.add.mockClear();
      refuse(NO_ANSWER);

      await vi.waitFor(() => expect(messageService.add).toHaveBeenCalledTimes(1));
      const toast = messageService.add.mock.calls[0][0] as { detail: string };
      expect(toast.detail).toContain('Ema Crossover');
      expect(toast.detail).toContain(NO_ANSWER_MESSAGE);
    });
  });
});
