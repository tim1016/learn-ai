import { fireEvent, render, screen, within } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { HttpErrorResponse } from '@angular/common/http';
import { TestBed, type ComponentFixture } from '@angular/core/testing';
import { afterAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { MessageService } from 'primeng/api';
import { of } from 'rxjs';
import type {
  HistoricalExecutionRecoveryPlan,
  SqliteRecoveryAction,
  SqliteRecoveryActionCheck,
  SqliteSafeFlattenPlan,
} from '../../../../api/alpaca.types';
import { BotPanelShellComponent } from './bot-panel-shell.component';
import { ActiveLensBridgeService } from '../../../../shared/lens/active-lens-bridge.service';
import { BrokerV2PanelService } from '../lib/broker-v2-panel.service';
import { BrokersService } from '../../../../services/brokers.service';
import { MarketDataService } from '../../../../services/market-data.service';
import { formatTimestampDisplay } from '../../../../shared/timestamp/timestamp-display';
import { fakeChartFeed } from '../../../../testing/bot-panel-fixtures';
import { DUAL_PANE_CHART_FACTORY } from '../dual-pane-chart/dual-pane-chart.component';
import type {
  BotPanelView,
  BotPanelLiveSnapshot,
  BotRunView,
  PanelAction,
  PanelActionResult,
  PanelProfile,
} from '../lib/broker-v2-panel.types';
import { provideRouter, Router } from '@angular/router';
import {
  provideFleetDirectory,
  testLane,
  type FleetDirectoryDouble,
} from '../../../../fleet/fleet-directory-testing';
import { LANE_FENCE_REFRESH_FAILED_MESSAGE } from '../../../../fleet/lane-fence';

const messageService = { add: vi.fn() };
const chartMocks = vi.hoisted(() => {
  const timeScale = { fitContent: vi.fn() };
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
      { provide: MarketDataService, useValue: marketDataMock },
    ],
  });
  chartMocks.createChart.mockClear();
});

const PROFILE: PanelProfile = {
  broker: 'alpaca',
  fee_fidelity: 'none',
  flatten_supported: false,
  live_bars_supported: false,
  stations: [],
  supported_action_ids: ['resume', 'stop'],
};

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
  resume_admission: null,
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
    resume_eligible: false,
    resume_label: 'Resume not applicable',
    resume_explanation: 'This strategy instance already has a live run.',
    carryover_checkpoint_exposure: {},
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
  primary_action_by_lens: { trader: null, operator: null },
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

const STOP_ACTION = {
  action_id: 'stop',
  revision: 1,
  concurrency_token: 'stop-token',
  enabled: true,
  label: 'Stop',
  explanation: 'Stop the bot.',
  blockers: [],
  confirmation: null,
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
    panel,
    live_chart: LIVE_CHART,
  };
}

function safeFlattenSnapshot(): BotPanelLiveSnapshot {
  return liveSnapshot({
    ...PANEL,
    revision: 17,
    actions: [PREPARE_SAFE_FLATTEN_ACTION],
    primary_action_by_lens: { trader: null, operator: 'prepare_safe_flatten' },
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

function extendedFlattenSnapshot(prepareToken = 'plan-token-17'): BotPanelLiveSnapshot {
  const snapshot = safeFlattenSnapshot();
  return {
    ...snapshot,
    panel: {
      ...snapshot.panel,
      actions: [
        { ...PREPARE_SAFE_FLATTEN_ACTION, concurrency_token: prepareToken },
        EXECUTE_SAFE_FLATTEN_ACTION,
      ],
    },
  };
}

function historicalRecoverySnapshot(): BotPanelLiveSnapshot {
  return liveSnapshot({
    ...PANEL,
    revision: 17,
    actions: [HISTORICAL_RECOVERY_ACTION],
    primary_action_by_lens: { trader: null, operator: 'recover_exact_execution_evidence' },
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
    primary_action_by_lens: { trader: null, operator: 'open_custody_timeline' },
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
    is_current: true,
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

const mockService = {
  getPanelProfile: vi.fn().mockResolvedValue(PROFILE),
  getPanel: vi.fn().mockResolvedValue(PANEL),
  getLiveSnapshot: vi.fn().mockResolvedValue(liveSnapshot()),
  liveStreamUrl: vi.fn().mockReturnValue('/api/test/live-stream'),
  getCurrentRun: vi.fn().mockResolvedValue(makeRun()),
  getRunHistory: vi.fn().mockResolvedValue({
    runs: [
      makeRun({
        run_id: 'run-previous',
        launch_reason: 'resume',
        started_at_ms: 1_753_700_000_000,
        is_current: false,
        process: null,
        terminal_outcome: {
          kind: 'STOPPED',
          reason_code: 'OPERATOR_STOP',
          recorded_at_ms: 1_753_750_000_000,
          run_id: 'run-previous',
        },
      }),
    ],
    next_cursor: null,
  }),
  getLiveChart: vi.fn().mockResolvedValue({
    strategy_instance_id: 'sid-001',
    symbol: 'QQQ',
    trading_date_open_ms: 1_753_800_000_000,
    trading_date_close_ms: 1_753_823_400_000,
    resolution: '1m',
    bars: [],
    fill_markers: [],
    overlay_notices: [],
    feed: fakeChartFeed(),
    as_of_ms: 1_753_800_000_000,
  }),
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
    read_by: 'operator:test',
    read_at_ms: 1_753_800_000_000,
  }),
  runBotAction: vi.fn().mockResolvedValue({
    action_id: 'resume',
    outcome: 'success',
    receipt_id: 'receipt-001',
    recorded_at_ms: 1_753_800_000_000,
    applied: true,
    revision: 1,
    concurrency_token: 'start-token',
    message: 'Bot start requested.',
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
  checkSqliteSafeFlatten: vi.fn().mockResolvedValue({
    capability: SAFE_FLATTEN_CAPABILITY,
    reduction_pricing: { kind: 'regular_session' },
  } satisfies SqliteRecoveryActionCheck),
};

const marketDataMock = {
  getStockSnapshot: vi.fn().mockReturnValue(of({
    success: true,
    snapshot: {
      ticker: 'QQQ',
      day: { open: 480, high: 482, low: 478, close: 481.42, volume: 1, vwap: 480 },
      prevDay: null,
      min: null,
      todaysChange: 2.42,
      todaysChangePercent: 0.51,
      updated: 1_753_800_001_000,
    },
    error: null,
  })),
};

// The switch itself now lives in the global top bar, outside this
// component's own render tree (ActiveLensBridgeService); this mirrors what
// it does — call the registered host's own select() — rather than driving
// the query param through `Router.navigate` directly, which a test that
// spies on `Router.navigate` for an unrelated assertion would neuter.
async function switchLens(
  fixture: ComponentFixture<BotPanelShellComponent>,
  lens: 'trader' | 'operator',
): Promise<void> {
  const bridge = TestBed.inject(ActiveLensBridgeService);
  await vi.waitFor(() => expect(bridge.host()).not.toBeNull());
  bridge.host()?.select(lens);
  await fixture.whenStable();
  fixture.detectChanges();
}

function openDisclosure(label: string): void {
  const details = screen.getByText(label).closest('details');
  if (details === null) throw new Error(`Expected ${label} disclosure.`);
  details.open = true;
  fireEvent(details, new Event('toggle'));
}

function fakeActionResult(overrides: Partial<PanelActionResult> = {}): PanelActionResult {
  return {
    action_id: 'stop',
    outcome: 'success',
    receipt_id: 'receipt-001',
    recorded_at_ms: 1_753_800_000_000,
    applied: true,
    revision: 1,
    concurrency_token: 'stop-token',
    message: 'Bot stop requested.',
    ...overrides,
  };
}

/** Renders the shell with a single "Stop" action available on the trader
 * lens, so the fence tests only need to click one button. */
async function renderShell(
  overrides: {
    directory?: FleetDirectoryDouble;
    runBotAction?: ReturnType<typeof vi.fn>;
  } = {},
) {
  // A persistent mock, not `mockResolvedValueOnce`: a rebind restarts the
  // live store (its address is a read and must follow the current lane, see
  // the constructor's second effect), which re-fetches the snapshot. A local
  // override — not a mutation of the shared `mockService.getLiveSnapshot` —
  // keeps every fetch, including that restart-triggered one, returning the
  // Stop-action panel without leaking a persistent mock into later tests.
  const service = {
    ...mockService,
    getLiveSnapshot: vi.fn().mockResolvedValue(liveSnapshot({
      ...PANEL,
      actions: [STOP_ACTION],
      primary_action_by_lens: { trader: 'stop', operator: 'stop' },
    })),
    ...(overrides.runBotAction ? { runBotAction: overrides.runBotAction } : {}),
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
  return { fixture };
}

describe('BotPanelShellComponent', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // The panel now shares the desk's stored lens preference; a test that
    // switched lenses must not leak that choice into the next render.
    localStorage.clear();
  });

  afterAll(() => {
    globalThis.EventSource = originalEventSource;
  });

  describe('the workspace tab this page belongs to', () => {
    // A bot's page sits inside the account workspace, under the tab it was
    // opened from (ADR 0064 Decision 1). The Gallery tile and the roster's
    // links stamp that tab on the URL; this is the reading half.
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

    it.each([
      ['?from=gallery', 'Gallery', 'gallery'],
      ['?from=bots', 'Bots', 'bots'],
      ['', 'Bots', 'bots'],
      ['?from=elsewhere', 'Bots', 'bots'],
    ])('points its way back at %s → %s', async (query, label, segment) => {
      await renderShellWithStamp(query);

      const back = screen.getByRole('link', { name: label });
      expect(back.getAttribute('href')).toBe(
        `/brokers/alpaca/clerks/clrk_spec/accounts/DUM284968/${segment}`,
      );
    });

    it('names the bot in its banner and registers as the global lens toggle host', async () => {
      const { container } = await renderShellWithStamp('?from=bots');

      expect(screen.getByRole('heading', { name: /Ema Crossover/ })).toBeTruthy();
      expect(container.querySelector('app-bot-banner')).not.toBeNull();
      // The choice of view onto this bot is the global top bar's toggle now
      // (ActiveLensBridgeService), not a nav row this page renders itself.
      expect(TestBed.inject(ActiveLensBridgeService).host()).not.toBeNull();
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

    function resumableSnapshot(): BotPanelLiveSnapshot {
      return liveSnapshot({
        ...PANEL,
        health: { ...PANEL.health, running: false },
        actions: [
          {
            action_id: 'resume',
            label: 'Resume',
            explanation: 'Resume evaluating bars.',
            enabled: true,
            blockers: [],
            confirmation: null,
            revision: 1,
            concurrency_token: 'start-token',
          },
        ],
        primary_action_by_lens: { trader: 'resume', operator: 'resume' },
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
      mockService.getLiveSnapshot.mockResolvedValueOnce(resumableSnapshot());
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
      expect(screen.getByRole('article', { name: 'Market tape for QQQ' })).toBeTruthy();
      expect(screen.getByRole('button', { name: 'Resume' })).toBeTruthy();

      emitOnLiveStream('snapshot', JSON.stringify(resumableSnapshot()));
      await fixture.whenStable();
      fixture.detectChanges();

      expect(screen.queryByRole('alert', { name: 'The live panel stopped updating.' })).toBeNull();
      expect(fixture.nativeElement.classList.contains('is-stale')).toBe(false);
    });

    it('keeps the action receipt when the post-action refresh finds the producer stalled', async () => {
      mockService.getLiveSnapshot
        .mockResolvedValueOnce(resumableSnapshot())
        .mockRejectedValueOnce(new HttpErrorResponse({ status: 503, error: { detail: STALL } }));
      const fixture = await renderShell();

      fireEvent.click(screen.getByRole('button', { name: 'Resume' }));
      for (let step = 0; step < 4; step += 1) {
        await fixture.whenStable();
        fixture.detectChanges();
      }

      expect(mockService.getLiveSnapshot).toHaveBeenCalledTimes(2);
      expect(screen.getByRole('alert', { name: 'The live panel stopped updating.' })).toBeTruthy();
      expect(screen.getByText('Bot start requested.')).toBeTruthy();
      expect(screen.getByText('receipt-001')).toBeTruthy();
      expect(screen.getByRole('button', { name: 'Resume' })).toBeTruthy();
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

      const notice = screen.getByRole('alert', { name: 'Chart feed stalled' });
      expect(within(notice).getByText('Do not read the chart as current.')).toBeTruthy();
      // The headline speaks for the bot's own line; the chart speaks for its own.
      expect(screen.getByText('Bot market data live')).toBeTruthy();
      expect(screen.queryByRole('alert', { name: 'The live panel stopped updating.' })).toBeNull();
      expect(fixture.nativeElement.classList.contains('is-stale')).toBe(false);
    });
  });

  it('shows loading state initially then renders the trader lens', async () => {
    const { fixture } = await render(BotPanelShellComponent, {
      inputs: { clerkId: 'clrk_spec', broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
      providers: [provideRouter([]), { provide: BrokerV2PanelService, useValue: mockService }, { provide: BrokersService, useValue: brokersMock },
        { provide: MessageService, useValue: messageService }],
    });

    // Wait for async initial load
    await fixture.whenStable();
    fixture.detectChanges();

    // The loaded panel drives the chart's instrument context.
    expect(screen.getByRole('article', { name: 'Market tape for QQQ' })).toBeTruthy();
    expect(mockService.getLiveSnapshot).toHaveBeenCalledWith(
      expect.objectContaining({
        broker: 'alpaca', clerkId: 'clrk_spec', accountId: 'DUM284968', entityId: 'sid-001',
      }),
      'sid-001',
      '5s',
    );
    expect(screen.queryByText('run-current')).toBeNull();
    expect(mockService.getRunHistory).not.toHaveBeenCalled();
    expect(mockService.getCurrentRun).toHaveBeenCalledTimes(1);
    const runTimes = within(fixture.nativeElement.querySelector('.run-timing'));
    expect(runTimes.getByText(
      formatTimestampDisplay(makeRun().started_at_ms, { granularity: 'time' }),
    )).toBeTruthy();

    await switchLens(fixture, 'operator');

    openDisclosure('Run evidence');
    await fixture.whenStable();
    fixture.detectChanges();

    expect(screen.getByText('run-current')).toBeTruthy();
  });

  it('refreshes and renders the safe-flatten plan without posting a panel mutation', async () => {
    mockService.getLiveSnapshot.mockResolvedValueOnce(safeFlattenSnapshot());
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

    await switchLens(fixture, 'operator');
    fireEvent.click(screen.getByRole('button', {
      name: /Ready Prepare safe flatten/i,
    }));
    await fixture.whenStable();
    fixture.detectChanges();
    fireEvent.click(await screen.findByRole('button', { name: 'Prepare safe flatten' }));

    const planRegion = await screen.findByRole('region', {
      name: 'Prepared safe-flatten reduction plan',
    });
    expect(within(planRegion).getByText('Qqq')).toBeTruthy();
    expect(within(planRegion).getByText('2.5')).toBeTruthy();
    expect(within(planRegion).getByText(/Inside the regular session this flatten is a market order/))
      .toBeTruthy();
    expect(screen.queryByRole('region', { name: 'Extended-hours flatten limit order' })).toBeNull();
    expect(brokersMock.checkSqliteSafeFlatten).toHaveBeenCalledWith(
      'clrk_spec',
      'DUM284968',
      { action_id: 'prepare_safe_flatten', concurrency_token: 'plan-token-17' },
      'sid-001',
    );
    expect(mockService.runBotAction).not.toHaveBeenCalled();

    fixture.componentRef.setInput('sid', 'sid-002');
    fixture.detectChanges();

    expect(screen.queryByRole('region', {
      name: 'Prepared safe-flatten reduction plan',
    })).toBeNull();
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

    await switchLens(fixture, 'operator');
    // The backend policy folds RecoveryCapability.primary into the Operator
    // reference (ADR 0027 precedence, #1665): the banner renders it once,
    // and the readiness accordion suppresses its own would-be duplicate row.
    expect(screen.getAllByRole('button', { name: 'Recover exact execution evidence' })).toHaveLength(1);
    fireEvent.click(screen.getByRole('button', { name: 'Recover exact execution evidence' }));
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
    expect(screen.getByRole('dialog').textContent).toContain('DUM284968');

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
    expect(mockService.confirmHistoricalExecutionRecovery.mock.calls[0][0])
      .toBe(mockService.prepareHistoricalExecutionRecovery.mock.calls[0][0]);
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

  it('deep-links the Account Desk with one exact, non-cross-correlated evidence identity', async () => {
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

    await switchLens(fixture, 'operator');
    fireEvent.click(screen.getByRole('button', { name: 'Open custody timeline' }));

    expect(navigate).toHaveBeenLastCalledWith(
      ['/brokers', 'alpaca', 'clerks', 'clrk_spec', 'accounts', 'DUM284968'], {
      queryParams: {
        lens: 'operator',
        timelineBot: 'sid-001',
        timelineUncertaintyId: 'uncertainty:17',
      },
      },
    );
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

    await switchLens(fixture, 'operator');
    fireEvent.click(screen.getByRole('button', { name: 'Recover exact execution evidence' }));
    await fixture.whenStable();
    fixture.detectChanges();

    expect(screen.getByText('The recovery evidence changed. Refresh before confirming the action.')).toBeTruthy();
    expect(screen.getByText('Stale Action Token')).toBeTruthy();
    expect(screen.queryByRole('heading', { name: 'Confirm exact execution recovery' })).toBeNull();
  });

  it('discards a safe-flatten response after route identity changes', async () => {
    const pendingCapability = deferred<SqliteRecoveryActionCheck>();
    mockService.getLiveSnapshot.mockResolvedValueOnce(safeFlattenSnapshot());
    brokersMock.checkSqliteSafeFlatten.mockReturnValueOnce(
      pendingCapability.promise,
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

    await switchLens(fixture, 'operator');
    fireEvent.click(screen.getByRole('button', {
      name: /Ready Prepare safe flatten/i,
    }));
    await fixture.whenStable();
    fixture.detectChanges();
    fireEvent.click(await screen.findByRole('button', { name: 'Prepare safe flatten' }));

    fixture.componentRef.setInput('sid', 'sid-002');
    fixture.detectChanges();
    pendingCapability.resolve({ capability: SAFE_FLATTEN_CAPABILITY, reduction_pricing: null });
    await fixture.whenStable();
    fixture.detectChanges();

    expect(screen.queryByRole('region', {
      name: 'Prepared safe-flatten reduction plan',
    })).toBeNull();
  });

  describe('an extended-hours safe flatten (#2007)', () => {
    /** Let a multi-step flow finish: a send refreshes the panel, posts, then refreshes again. */
    async function settle(fixture: ComponentFixture<BotPanelShellComponent>): Promise<void> {
      for (let step = 0; step < 4; step += 1) {
        await fixture.whenStable();
        fixture.detectChanges();
      }
    }

    async function prepareFlatten(): Promise<ComponentFixture<BotPanelShellComponent>> {
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
      await switchLens(fixture, 'operator');
      fireEvent.click(screen.getByRole('button', { name: /Ready Prepare safe flatten/i }));
      await fixture.whenStable();
      fixture.detectChanges();
      fireEvent.click(await screen.findByRole('button', { name: 'Prepare safe flatten' }));
      await fixture.whenStable();
      fixture.detectChanges();
      return fixture;
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
      brokersMock.checkSqliteSafeFlatten.mockResolvedValueOnce({
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
      brokersMock.checkSqliteSafeFlatten.mockResolvedValueOnce({
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
      expect(within(ticket).getByText('Pre-market')).toBeTruthy();
      expect(within(ticket).getByText(/\$480\.10/)).toBeTruthy();
      expect(within(ticket).getByText(/\$480\.20/)).toBeTruthy();
      fireEvent.click(within(ticket).getByRole('button', { name: 'Review limit order' }));
      await settle(fixture);
      expect(within(ticket).getByText(/at limit \$479\.13/)).toBeTruthy();
      expect(brokersMock.checkSqliteSafeFlatten).toHaveBeenLastCalledWith(
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
      expect(mockService.runBotAction).not.toHaveBeenCalled();
      expect(screen.queryByRole('region', { name: 'Prepared safe-flatten reduction plan' }))
        .toBeNull();
      expect(screen.getByText(/Limit order sent at \$479\.13/)).toBeTruthy();
    });

    it('takes the execute token from a live panel read while the live projection is stalled (#2353)', async () => {
      const observedAtMs = Date.now();
      mockService.getLiveSnapshot.mockResolvedValue(extendedFlattenSnapshot());
      brokersMock.checkSqliteSafeFlatten.mockResolvedValue(EXTENDED_CHECK_WITH_READING(observedAtMs));
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
      expect(screen.getByText(/Limit order sent at \$479\.13/)).toBeTruthy();
    });

    it('never says an order was sent when none reached the broker', async () => {
      // A durably accepted EXIT whose reducing order is still pending — a
      // broker lookup outage, a transiently blocked REDUCE — has sent nothing,
      // and telling the operator otherwise is the one thing worse than the
      // outage itself (Codex review 2026-09-19).
      const observedAtMs = Date.now();
      mockService.getLiveSnapshot.mockResolvedValue(extendedFlattenSnapshot());
      brokersMock.checkSqliteSafeFlatten.mockResolvedValue(EXTENDED_CHECK_WITH_READING(observedAtMs));
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

      expect(screen.getByText(/no order has reached the broker yet/)).toBeTruthy();
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
      brokersMock.checkSqliteSafeFlatten.mockResolvedValueOnce(extendedCheck);
      const fixture = await prepareFlatten();
      brokersMock.checkSqliteSafeFlatten.mockRejectedValueOnce(
        new HttpErrorResponse({
          status: 409,
          error: { detail: { reason: 'stale_action_token', message: 'The plan changed.' } },
        }),
      );
      brokersMock.checkSqliteSafeFlatten.mockResolvedValueOnce(extendedCheck);

      const ticket = await screen.findByRole('region', { name: 'Extended-hours flatten limit order' });
      fireEvent.click(within(ticket).getByRole('button', { name: 'Refresh quote' }));
      await settle(fixture);

      // Prepare, the refused refresh, then the retry — each with whatever
      // token the panel presented at the time.
      expect(brokersMock.checkSqliteSafeFlatten).toHaveBeenCalledTimes(3);
      const tokens = brokersMock.checkSqliteSafeFlatten.mock.calls.map(
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

    it('names when the next session opens instead of offering a ticket', async () => {
      mockService.getLiveSnapshot.mockResolvedValueOnce(extendedFlattenSnapshot());
      brokersMock.checkSqliteSafeFlatten.mockResolvedValueOnce({
        capability: SAFE_FLATTEN_CAPABILITY,
        reduction_pricing: {
          kind: 'refused',
          reason_code: 'NO_SESSION_OPEN',
          explanation: 'No trading session is open now, so no reduction can be sent.',
          next_step: 'Flatten again once the next session opens.',
          available_at_ms: 1_753_862_400_000,
        },
      } satisfies SqliteRecoveryActionCheck);
      await prepareFlatten();

      const plan = await screen.findByRole('region', { name: 'Prepared safe-flatten reduction plan' });
      expect(within(plan).getByText(/No trading session is open now/)).toBeTruthy();
      expect(within(plan).getByText(
        formatTimestampDisplay(1_753_862_400_000, { mode: 'et' }),
      )).toBeTruthy();
      expect(screen.queryByRole('region', { name: 'Extended-hours flatten limit order' })).toBeNull();
    });
  });

  it('renders the bot banner once, not re-mounted inside a lens, registers one lens-toggle host, and keeps run evidence out of Trader', async () => {
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

    const banners = container.querySelectorAll('app-bot-banner');
    const nestedInLens = container.querySelector('app-trader-lens app-bot-banner, app-operator-lens app-bot-banner');
    expect(banners).toHaveLength(1);
    expect(nestedInLens).toBeNull();
    // No local Trader/Operator tab UI renders here any more — the global top
    // bar owns it, and this page is registered as its one host
    // (ActiveLensBridgeService). (The market-data source switcher is a
    // different, unrelated tablist the trader lens itself still owns.)
    expect(screen.queryByRole('tab', { name: 'Trader' })).toBeNull();
    expect(screen.queryByRole('tab', { name: 'Operator' })).toBeNull();
    expect(TestBed.inject(ActiveLensBridgeService).host()).not.toBeNull();
    expect(screen.queryByText('Run evidence')).toBeNull();
    expect(screen.queryByText('Strategy evidence')).toBeNull();
    expect(screen.queryByText('Clerk evidence')).toBeNull();
  });

  it('keeps the market snapshot mounted while switching lenses', async () => {
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

    expect(marketDataMock.getStockSnapshot).toHaveBeenCalledTimes(1);

    await switchLens(fixture, 'operator');
    await switchLens(fixture, 'trader');

    expect(marketDataMock.getStockSnapshot).toHaveBeenCalledTimes(1);
  });

  describe('market tape price (#2407)', () => {
    function tapeSnapshot(day: number, min: number | null) {
      const bar = (close: number) => ({ open: close, high: close, low: close, close, volume: 0, vwap: close });
      return of({
        success: true,
        snapshot: {
          ticker: 'QQQ',
          day: bar(day),
          prevDay: bar(515),
          min: min === null ? null : { ...bar(min), accumulatedVolume: 0, timestamp: null },
          todaysChange: -2.06,
          todaysChangePercent: -0.4,
          updated: 1_753_800_001_000,
        },
        error: null,
      });
    }

    async function renderTape() {
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
      return fixture;
    }

    const tapePrices = () =>
      [...document.querySelectorAll('.ticker-quote__price')].map((el) => el.textContent?.trim());

    beforeEach(() => marketDataMock.getStockSnapshot.mockClear());

    it('does not render a zero day bar as a $0.00 price', async () => {
      marketDataMock.getStockSnapshot.mockReturnValueOnce(tapeSnapshot(0, 0));

      await renderTape();

      expect(tapePrices()).toEqual([]);
      expect(screen.queryByText('-0.40%')).toBeNull();
    });

    it('falls back to the minute bar while the day bar is still zero', async () => {
      marketDataMock.getStockSnapshot.mockReturnValueOnce(tapeSnapshot(0, 512.34));

      await renderTape();

      expect(tapePrices()).toContain('$512.34');
    });

    it('re-reads the snapshot so an empty read at the open heals', async () => {
      // The panel's live store does not load under fake timers, so the test
      // fires the shell's own snapshot cadence directly.
      const setIntervalSpy = vi.spyOn(globalThis, 'setInterval');
      try {
        marketDataMock.getStockSnapshot
          .mockReturnValueOnce(tapeSnapshot(0, 0))
          .mockReturnValueOnce(tapeSnapshot(513.21, 513.1));
        const fixture = await renderTape();
        expect(tapePrices()).toEqual([]);
        const refresh = setIntervalSpy.mock.calls.find(([, ms]) => ms === 60_000)?.[0];
        if (typeof refresh !== 'function') throw new Error('Expected a 60 s snapshot refresh.');

        refresh();
        await fixture.whenStable();
        fixture.detectChanges();

        expect(marketDataMock.getStockSnapshot).toHaveBeenCalledTimes(2);
        expect(tapePrices()).toContain('$513.21');
      } finally {
        setIntervalSpy.mockRestore();
      }
    });
  });

  it('loads previous runs only while the operator lens is mounted', async () => {
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

    expect(mockService.getRunHistory).not.toHaveBeenCalled();
    await switchLens(fixture, 'operator');
    openDisclosure('Run evidence');
    await fixture.whenStable();
    fixture.detectChanges();
    fireEvent.click(screen.getByRole('button', { name: 'Previous Runs' }));
    await fixture.whenStable();
    fixture.detectChanges();

    expect(mockService.getRunHistory).toHaveBeenCalledWith(
      expect.objectContaining({
        broker: 'alpaca', clerkId: 'clrk_spec', accountId: 'DUM284968', entityId: 'sid-001',
      }),
      'sid-001',
      undefined,
    );
    expect(screen.getByText('run-previous')).toBeTruthy();
    expect(mockService.getCurrentRun).toHaveBeenCalledTimes(1);
    // The banner's own Started time is the current run's, never clobbered by
    // the previous-run fetch the disclosure just made.
    const bannerRunTimes = within(fixture.nativeElement.querySelector('.run-timing'));
    expect(bannerRunTimes.getByText(
      formatTimestampDisplay(makeRun().started_at_ms, { granularity: 'time' }),
    )).toBeTruthy();
    expect(bannerRunTimes.queryByText(
      formatTimestampDisplay(1_753_700_000_000, { granularity: 'time' }),
    )).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Previous Runs' }));
    await fixture.whenStable();
    expect(mockService.getRunHistory).toHaveBeenCalledTimes(1);

    await switchLens(fixture, 'trader');

    expect(screen.queryByText('run-previous')).toBeNull();

    await switchLens(fixture, 'operator');

    expect(screen.queryByText('run-previous')).toBeNull();
    expect(mockService.getRunHistory).toHaveBeenCalledTimes(1);

    openDisclosure('Run evidence');
    await fixture.whenStable();
    fixture.detectChanges();
    fireEvent.click(screen.getByRole('button', { name: 'Previous Runs' }));
    await fixture.whenStable();
    fixture.detectChanges();

    expect(mockService.getRunHistory).toHaveBeenCalledTimes(2);
    expect(screen.getByText('run-previous')).toBeTruthy();
  });

  it('requests one older run at a time with the server-issued cursor', async () => {
    mockService.getRunHistory
      .mockResolvedValueOnce({
        runs: [
          makeRun({
            run_id: 'run-newest-previous',
            launch_reason: 'resume',
            started_at_ms: 1_753_700_000_000,
            is_current: false,
            process: null,
          }),
        ],
        next_cursor: 'run-newest-previous',
      })
      .mockResolvedValueOnce({
        runs: [
          makeRun({
            run_id: 'run-older',
            started_at_ms: 1_753_600_000_000,
            is_current: false,
            process: null,
          }),
        ],
        next_cursor: null,
      });
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
    await switchLens(fixture, 'operator');
    openDisclosure('Run evidence');
    await fixture.whenStable();
    fixture.detectChanges();
    fireEvent.click(screen.getByRole('button', { name: 'Previous Runs' }));
    await fixture.whenStable();
    fixture.detectChanges();

    fireEvent.click(screen.getByRole('button', { name: 'Older run' }));
    await fixture.whenStable();
    fixture.detectChanges();

    expect(mockService.getRunHistory).toHaveBeenLastCalledWith(
      expect.objectContaining({
        broker: 'alpaca', clerkId: 'clrk_spec', accountId: 'DUM284968', entityId: 'sid-001',
      }),
      'sid-001',
      'run-newest-previous',
    );
    expect(screen.getByText('run-older')).toBeTruthy();
  });

  it('keeps lifecycle actions bound to the current instance while viewing history', async () => {
    mockService.getLiveSnapshot.mockResolvedValueOnce(liveSnapshot({
      ...PANEL,
      health: { ...PANEL.health, running: false },
      actions: [
        {
          action_id: 'resume',
          label: 'Resume',
          explanation: 'Resume evaluating bars.',
          enabled: true,
          blockers: [],
          confirmation: null,
          revision: 1,
          concurrency_token: 'start-token',
        },
      ],
      primary_action_by_lens: { trader: 'resume', operator: 'resume' },
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
    await switchLens(fixture, 'operator');
    openDisclosure('Run evidence');
    await fixture.whenStable();
    fixture.detectChanges();
    fireEvent.click(screen.getByRole('button', { name: 'Previous Runs' }));
    await fixture.whenStable();
    fixture.detectChanges();

    await switchLens(fixture, 'trader');
    fireEvent.click(screen.getByRole('button', { name: 'Resume' }));
    await fixture.whenStable();

    expect(mockService.runBotAction).toHaveBeenCalledWith(
      expect.objectContaining({
        broker: 'alpaca', clerkId: 'clrk_spec', accountId: 'DUM284968', entityId: 'sid-001',
      }),
      'sid-001',
      expect.objectContaining({ action_id: 'resume' }),
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
    mockService.getCurrentRun
      .mockResolvedValueOnce(makeRun())
      .mockResolvedValueOnce(
        makeRun({
          process: null,
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

    await switchLens(fixture, 'operator');
    openDisclosure('Run evidence');
    await fixture.whenStable();
    fixture.detectChanges();
    expect(screen.getByText('No terminal evidence recorded')).toBeTruthy();
    await new Promise((resolve) => setTimeout(resolve, 5_100));
    await fixture.whenStable();
    fixture.detectChanges();

    expect(screen.getByText('Stopped')).toBeTruthy();
    fixture.destroy();
  }, 10_000);

  it('does not poll immutable current-run evidence while the bot is off duty', async () => {
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

    await switchLens(fixture, 'operator');

    expect(mockService.getCurrentRun).toHaveBeenCalledTimes(1);
    openDisclosure('Run evidence');
    await fixture.whenStable();
    fixture.detectChanges();
    expect(mockService.getCurrentRun).toHaveBeenCalledTimes(1);
    await new Promise((resolve) => setTimeout(resolve, 5_100));
    expect(mockService.getCurrentRun).toHaveBeenCalledTimes(1);
    fixture.destroy();
  }, 10_000);

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

  it('registers with the global lens toggle while loaded, and its choice updates the query string', async () => {
    // The tab UI itself moved to the global top bar (ActiveLensBridgeService);
    // keyboard/roving-tabindex behavior is covered by lens-tabs.component.spec.
    // What this shell still owns: registering while it has content to switch,
    // and persisting the toggle's choice into `?lens=`.
    const { fixture } = await render(BotPanelShellComponent, {
      inputs: { clerkId: 'clrk_spec', broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
      providers: [provideRouter([]), { provide: BrokerV2PanelService, useValue: mockService }, { provide: BrokersService, useValue: brokersMock },
        { provide: MessageService, useValue: messageService }],
    });
    await fixture.whenStable();
    fixture.detectChanges();

    const bridge = TestBed.inject(ActiveLensBridgeService);
    const host = bridge.host();
    if (host === null) throw new Error('Expected the shell to register as the active lens host.');
    expect(host.lens()).toBe('trader');

    host.select('operator');
    await fixture.whenStable();
    fixture.detectChanges();

    expect(bridge.host()?.lens()).toBe('operator');
    expect(fixture.debugElement.injector.get(Router).url).toContain('lens=operator');
  });

  it('fetches a new server projection for a selected transaction', async () => {
    const { fixture } = await render(BotPanelShellComponent, {
      inputs: { clerkId: 'clrk_spec', broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
      providers: [provideRouter([]), { provide: BrokerV2PanelService, useValue: mockService }, { provide: BrokersService, useValue: brokersMock },
        { provide: MessageService, useValue: messageService }],
    });
    await fixture.whenStable();
    await switchLens(fixture, 'operator');

    openDisclosure('Audit trail');
    await fixture.whenStable();
    fixture.detectChanges();
    fireEvent.click(screen.getByRole('button', { name: /Submit acknowledged at/ }));
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
      actions: [
        {
          action_id: 'resume',
          label: 'Resume',
          explanation: 'Resume evaluating bars.',
          enabled: true,
          blockers: [],
          confirmation: null,
          revision: 1,
          concurrency_token: 'start-token',
        },
      ],
      primary_action_by_lens: { trader: 'resume', operator: 'resume' },
    }));
    const { fixture } = await render(BotPanelShellComponent, {
      inputs: { clerkId: 'clrk_spec', broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
      providers: [provideRouter([]), { provide: BrokerV2PanelService, useValue: mockService }, { provide: BrokersService, useValue: brokersMock },
        { provide: MessageService, useValue: messageService }],
    });
    await fixture.whenStable();
    fixture.detectChanges();

    fireEvent.click(screen.getByRole('button', { name: 'Resume' }));
    await fixture.whenStable();
    fixture.detectChanges();

    expect(screen.getByText('Bot start requested.')).toBeTruthy();
    expect(screen.getByText('receipt-001')).toBeTruthy();
    expect(messageService.add).toHaveBeenCalledWith(
      expect.objectContaining({ severity: 'success', detail: 'Bot start requested.' }),
    );
  });

  it('renders backend-authored remediation for an unknown action outcome', async () => {
    mockService.getLiveSnapshot.mockResolvedValueOnce(liveSnapshot({
      ...PANEL,
      health: { ...PANEL.health, running: false },
      actions: [
        {
          action_id: 'resume',
          label: 'Resume',
          explanation: 'Resume evaluating bars.',
          enabled: true,
          blockers: [],
          confirmation: null,
          revision: 1,
          concurrency_token: 'start-token',
        },
      ],
      primary_action_by_lens: { trader: 'resume', operator: 'resume' },
    }));
    mockService.runBotAction.mockRejectedValueOnce(
      new HttpErrorResponse({
        status: 500,
        error: {
          detail: {
            action_id: 'resume',
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

    fireEvent.click(screen.getByRole('button', { name: 'Resume' }));
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
    // A rejected action refreshes the panel so a superseded "Ready to resume"
    // state doesn't linger (the 2026-08-04 val-nvda-0804-05 409).
    expect(mockService.getLiveSnapshot).toHaveBeenCalledTimes(2);
  });

  it('renders a receiptLabel-formatted reason_code when the backend sends no why prose', async () => {
    mockService.getLiveSnapshot.mockResolvedValueOnce(liveSnapshot({
      ...PANEL,
      health: { ...PANEL.health, running: false },
      actions: [
        {
          action_id: 'resume',
          label: 'Resume',
          explanation: 'Resume evaluating bars.',
          enabled: true,
          blockers: [],
          confirmation: null,
          revision: 1,
          concurrency_token: 'start-token',
        },
      ],
      primary_action_by_lens: { trader: 'resume', operator: 'resume' },
    }));
    mockService.runBotAction.mockRejectedValueOnce(
      new HttpErrorResponse({
        status: 409,
        error: {
          detail: {
            action_id: 'resume',
            outcome: 'failure',
            receipt_id: null,
            recorded_at_ms: 1_753_800_000_000,
            message: 'Resume is no longer available for this bot.',
            why: null,
            reason_code: 'TERMINAL_EVIDENCE_UNREADABLE',
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

    fireEvent.click(screen.getByRole('button', { name: 'Resume' }));
    await fixture.whenStable();
    fixture.detectChanges();

    expect(screen.getByText('Resume is no longer available for this bot.')).toBeTruthy();
    expect(screen.getByText('Terminal Evidence Unreadable')).toBeTruthy();
  });

  it('renders a cleanup-proven activation failure as Failure, distinct from Unknown', async () => {
    // PRD #1716 FR-6: outcome=failure (cleanup proven) must read differently
    // from outcome=unknown (unproven cleanup) — both the headline label and
    // the backend-authored prose distinguish failed-but-safe from unresolved.
    mockService.getLiveSnapshot.mockResolvedValueOnce(liveSnapshot({
      ...PANEL,
      health: { ...PANEL.health, running: false },
      actions: [
        {
          action_id: 'resume',
          label: 'Resume',
          explanation: 'Resume evaluating bars.',
          enabled: true,
          blockers: [],
          confirmation: null,
          revision: 1,
          concurrency_token: 'start-token',
        },
      ],
      primary_action_by_lens: { trader: 'resume', operator: 'resume' },
    }));
    mockService.runBotAction.mockRejectedValueOnce(
      new HttpErrorResponse({
        status: 500,
        error: {
          detail: {
            action_id: 'resume',
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

    fireEvent.click(screen.getByRole('button', { name: 'Resume' }));
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

    await userEvent.click(screen.getByRole('button', { name: /stop/i }));
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

    await userEvent.click(screen.getByRole('button', { name: /stop/i }));
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

    await userEvent.click(screen.getByRole('button', { name: /stop/i }));

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

    await userEvent.click(screen.getByRole('button', { name: /stop/i }));

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

    await userEvent.click(screen.getByRole('button', { name: /stop/i }));

    expect(runBotAction).toHaveBeenCalledWith(
      expect.objectContaining({ bindingGeneration: 3, routingEpoch: 4 }),
      expect.anything(), expect.anything(), null,
    );
  });
});
