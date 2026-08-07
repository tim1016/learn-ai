import { fireEvent, render, screen, within } from '@testing-library/angular';
import { HttpErrorResponse } from '@angular/common/http';
import { afterAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { MessageService } from 'primeng/api';
import type {
  SqliteRecoveryAction,
  SqliteSafeFlattenPlan,
} from '../../../../api/alpaca.types';
import { BotPanelShellComponent } from './bot-panel-shell.component';
import { BrokerV2PanelService } from '../lib/broker-v2-panel.service';
import { BrokersService } from '../../../../services/brokers.service';
import type {
  BotPanelView,
  BotPanelLiveSnapshot,
  BotRunView,
  PanelAction,
  PanelProfile,
} from '../lib/broker-v2-panel.types';
import { provideRouter, Router } from '@angular/router';

const messageService = { add: vi.fn() };

// DualPaneChartComponent -> lightweight-charts: mock for unit tests.
vi.mock('lightweight-charts', () => {
  const mockTimeScale = { fitContent: vi.fn() };
  const createMockSeries = () => ({ setData: vi.fn(), update: vi.fn(), applyOptions: vi.fn() });
  const createSeriesMarkers = vi.fn().mockReturnValue({ setMarkers: vi.fn() });
  const createMockChart = () => ({
    addSeries: vi.fn().mockReturnValue(createMockSeries()),
    timeScale: vi.fn().mockReturnValue(mockTimeScale),
    applyOptions: vi.fn(),
    remove: vi.fn(),
  });
  return {
    createChart: vi.fn().mockImplementation(() => createMockChart()),
    createSeriesMarkers,
    CandlestickSeries: 'CandlestickSeries',
  };
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
  updated_at_ms: 1_753_800_000_000,
  revision: 1,
  market_pulse: {
    session: 'OPEN',
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
  journal_tail_ref: '/api/brokers/alpaca/accounts/DUM284968/bots/sid-001/journal',
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
  realized_pnl_today: 0.0,
  open_pnl: null,
};

const SAFE_FLATTEN_PLAN: SqliteSafeFlattenPlan = {
  version_token: 'plan-token-17',
  account_id: 'DUM284968',
  authority_generation: 4,
  db_identity_token: 'db-generation-4',
  control_revision: 17,
  scope: 'BOT',
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

const SAFE_FLATTEN_CAPABILITY: SqliteRecoveryAction = {
  action_id: 'prepare_safe_flatten',
  label: 'Prepare safe flatten',
  explanation: 'Prepare a fresh reduction plan without submitting an order.',
  available: true,
  unavailable_reason_code: null,
  unavailable_reason: null,
  scope: 'BOT',
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
    readiness_checks: [{
      operation: PREPARE_SAFE_FLATTEN_ACTION.action_id,
      label: PREPARE_SAFE_FLATTEN_ACTION.label,
      ready: true,
      scope: 'bot',
      authority: 'Alpaca SQLite Clerk',
      explanation: PREPARE_SAFE_FLATTEN_ACTION.explanation,
      evidence: {},
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
  addEventListener = vi.fn();
  close = vi.fn();

  constructor(readonly url: string) {}
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
    as_of_ms: 1_753_800_000_000,
  }),
  getHistoryChart: vi.fn().mockResolvedValue({
    strategy_instance_id: 'sid-001',
    symbol: 'QQQ',
    preset: '1D',
    aggregation: '1m',
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
};

const brokersMock = {
  checkSqliteRecoveryAction: vi.fn().mockResolvedValue(SAFE_FLATTEN_CAPABILITY),
};

function openDisclosure(label: string): void {
  const details = screen.getByText(label).closest('details');
  if (details === null) throw new Error(`Expected ${label} disclosure.`);
  details.open = true;
  fireEvent(details, new Event('toggle'));
}

describe('BotPanelShellComponent', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterAll(() => {
    globalThis.EventSource = originalEventSource;
  });

  it('shows loading state initially then renders the trader lens', async () => {
    const { fixture } = await render(BotPanelShellComponent, {
      inputs: { broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
      providers: [provideRouter([]), { provide: BrokerV2PanelService, useValue: mockService }, { provide: BrokersService, useValue: brokersMock },
        { provide: MessageService, useValue: messageService }],
    });

    // Wait for async initial load
    await fixture.whenStable();
    fixture.detectChanges();

    // Symbol from the loaded panel should appear
    expect(screen.getByRole('heading', { name: 'QQQ', level: 1 })).toBeTruthy();
    expect(mockService.getLiveSnapshot).toHaveBeenCalledWith(
      'alpaca',
      'DUM284968',
      'sid-001',
      '5s',
    );
    expect(screen.queryByText('run-current')).toBeNull();
    expect(mockService.getRunHistory).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('tab', { name: 'Operator' }));
    await fixture.whenStable();
    fixture.detectChanges();

    openDisclosure('Run evidence');
    await fixture.whenStable();
    fixture.detectChanges();

    expect(screen.getByText('run-current')).toBeTruthy();
  });

  it('refreshes and renders the safe-flatten plan without posting a panel mutation', async () => {
    mockService.getLiveSnapshot.mockResolvedValueOnce(safeFlattenSnapshot());
    const { fixture } = await render(BotPanelShellComponent, {
      inputs: { broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
      providers: [
        provideRouter([]),
        { provide: BrokerV2PanelService, useValue: mockService },
        { provide: BrokersService, useValue: brokersMock },
        { provide: MessageService, useValue: messageService },
      ],
    });
    await fixture.whenStable();
    fixture.detectChanges();

    fireEvent.click(screen.getByRole('tab', { name: 'Operator' }));
    await fixture.whenStable();
    fixture.detectChanges();
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
    expect(brokersMock.checkSqliteRecoveryAction).toHaveBeenCalledWith(
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

  it('discards a safe-flatten response after route identity changes', async () => {
    const pendingCapability = deferred<SqliteRecoveryAction>();
    mockService.getLiveSnapshot.mockResolvedValueOnce(safeFlattenSnapshot());
    brokersMock.checkSqliteRecoveryAction.mockReturnValueOnce(
      pendingCapability.promise,
    );
    const { fixture } = await render(BotPanelShellComponent, {
      inputs: { broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
      providers: [
        provideRouter([]),
        { provide: BrokerV2PanelService, useValue: mockService },
        { provide: BrokersService, useValue: brokersMock },
        { provide: MessageService, useValue: messageService },
      ],
    });
    await fixture.whenStable();
    fixture.detectChanges();

    fireEvent.click(screen.getByRole('tab', { name: 'Operator' }));
    await fixture.whenStable();
    fixture.detectChanges();
    fireEvent.click(screen.getByRole('button', {
      name: /Ready Prepare safe flatten/i,
    }));
    await fixture.whenStable();
    fixture.detectChanges();
    fireEvent.click(await screen.findByRole('button', { name: 'Prepare safe flatten' }));

    fixture.componentRef.setInput('sid', 'sid-002');
    fixture.detectChanges();
    pendingCapability.resolve(SAFE_FLATTEN_CAPABILITY);
    await fixture.whenStable();
    fixture.detectChanges();

    expect(screen.queryByRole('region', {
      name: 'Prepared safe-flatten reduction plan',
    })).toBeNull();
  });

  it('keeps lens navigation above the hero and run evidence out of Trader', async () => {
    const { fixture, container } = await render(BotPanelShellComponent, {
      inputs: { broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
      providers: [
        provideRouter([]),
        { provide: BrokerV2PanelService, useValue: mockService },
        { provide: BrokersService, useValue: brokersMock },
        { provide: MessageService, useValue: messageService },
      ],
    });
    await fixture.whenStable();
    fixture.detectChanges();

    const navigation = container.querySelector('.lens-navigation');
    const hero = container.querySelector('app-panel-header');
    expect(navigation).not.toBeNull();
    expect(hero).not.toBeNull();
    if (navigation === null || hero === null) {
      throw new Error('Expected lens navigation and panel hero to render.');
    }
    expect(
      navigation.compareDocumentPosition(hero) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(screen.queryByText('Run evidence')).toBeNull();
    expect(screen.queryByText('Strategy evidence')).toBeNull();
    expect(screen.queryByText('Clerk evidence')).toBeNull();
  });

  it('loads previous runs only while the operator lens is mounted', async () => {
    const { fixture } = await render(BotPanelShellComponent, {
      inputs: { broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
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
    fireEvent.click(screen.getByRole('tab', { name: 'Operator' }));
    await fixture.whenStable();
    fixture.detectChanges();
    openDisclosure('Run evidence');
    await fixture.whenStable();
    fixture.detectChanges();
    fireEvent.click(screen.getByRole('button', { name: 'Previous Runs' }));
    await fixture.whenStable();
    fixture.detectChanges();

    expect(mockService.getRunHistory).toHaveBeenCalledWith(
      'alpaca',
      'sid-001',
      undefined,
    );
    expect(screen.getByText('run-previous')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'Previous Runs' }));
    await fixture.whenStable();
    expect(mockService.getRunHistory).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole('tab', { name: 'Trader' }));
    await fixture.whenStable();
    fixture.detectChanges();

    expect(screen.queryByText('run-previous')).toBeNull();

    fireEvent.click(screen.getByRole('tab', { name: 'Operator' }));
    await fixture.whenStable();
    fixture.detectChanges();

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
      inputs: { broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
      providers: [
        provideRouter([]),
        { provide: BrokerV2PanelService, useValue: mockService },
        { provide: BrokersService, useValue: brokersMock },
        { provide: MessageService, useValue: messageService },
      ],
    });
    await fixture.whenStable();
    fireEvent.click(screen.getByRole('tab', { name: 'Operator' }));
    await fixture.whenStable();
    fixture.detectChanges();
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
      'alpaca',
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
    }));
    const { fixture } = await render(BotPanelShellComponent, {
      inputs: { broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
      providers: [
        provideRouter([]),
        { provide: BrokerV2PanelService, useValue: mockService },
        { provide: BrokersService, useValue: brokersMock },
        { provide: MessageService, useValue: messageService },
      ],
    });
    await fixture.whenStable();
    fireEvent.click(screen.getByRole('tab', { name: 'Operator' }));
    await fixture.whenStable();
    fixture.detectChanges();
    openDisclosure('Run evidence');
    await fixture.whenStable();
    fixture.detectChanges();
    fireEvent.click(screen.getByRole('button', { name: 'Previous Runs' }));
    await fixture.whenStable();
    fixture.detectChanges();

    fireEvent.click(screen.getByRole('tab', { name: 'Trader' }));
    await fixture.whenStable();
    fixture.detectChanges();
    fireEvent.click(screen.getByRole('button', { name: 'Resume' }));
    await fixture.whenStable();

    expect(mockService.runBotAction).toHaveBeenCalledWith(
      'alpaca',
      'DUM284968',
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
        inputs: { broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
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
      inputs: { broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
      providers: [
        provideRouter([]),
        { provide: BrokerV2PanelService, useValue: mockService },
        { provide: BrokersService, useValue: brokersMock },
        { provide: MessageService, useValue: messageService },
      ],
    });
    await fixture.whenStable();
    fixture.detectChanges();

    fireEvent.click(screen.getByRole('tab', { name: 'Operator' }));
    await fixture.whenStable();
    fixture.detectChanges();
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
      inputs: { broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
      providers: [
        provideRouter([]),
        { provide: BrokerV2PanelService, useValue: mockService },
        { provide: BrokersService, useValue: brokersMock },
        { provide: MessageService, useValue: messageService },
      ],
    });
    await fixture.whenStable();
    fixture.detectChanges();

    fireEvent.click(screen.getByRole('tab', { name: 'Operator' }));
    await fixture.whenStable();
    fixture.detectChanges();

    expect(mockService.getCurrentRun).toHaveBeenCalledTimes(0);
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
      inputs: { broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
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
      inputs: { broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
      providers: [provideRouter([]), { provide: BrokerV2PanelService, useValue: mockService }, { provide: BrokersService, useValue: brokersMock },
        { provide: MessageService, useValue: messageService }],
    });

    await fixture.whenStable();
    fixture.detectChanges();

    expect(screen.getByRole('alert').textContent).toBe('Network error');
  });

  it('persists keyboard lens changes in the query string', async () => {
    const { fixture } = await render(BotPanelShellComponent, {
      inputs: { broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
      providers: [provideRouter([]), { provide: BrokerV2PanelService, useValue: mockService }, { provide: BrokersService, useValue: brokersMock },
        { provide: MessageService, useValue: messageService }],
    });
    await fixture.whenStable();
    fixture.detectChanges();

    fireEvent.keyDown(screen.getByRole('tab', { name: 'Trader' }), {
      key: 'ArrowRight',
    });
    await fixture.whenStable();
    fixture.detectChanges();

    expect(screen.getByRole('tab', { name: 'Operator' }).getAttribute('aria-selected')).toBe('true');
    expect(fixture.debugElement.injector.get(Router).url).toContain('lens=operator');
  });

  it('fetches a new server projection for a selected transaction', async () => {
    const { fixture } = await render(BotPanelShellComponent, {
      inputs: { broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
      providers: [provideRouter([]), { provide: BrokerV2PanelService, useValue: mockService }, { provide: BrokersService, useValue: brokersMock },
        { provide: MessageService, useValue: messageService }],
    });
    await fixture.whenStable();
    fireEvent.click(screen.getByRole('tab', { name: 'Operator' }));
    await fixture.whenStable();
    fixture.detectChanges();

    openDisclosure('Audit trail');
    await fixture.whenStable();
    fixture.detectChanges();
    fireEvent.click(screen.getByRole('button', { name: /Submit acknowledged at/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Select transaction tx-001 on rail' }));
    await fixture.whenStable();

    expect(mockService.getPanel).toHaveBeenLastCalledWith(
      'alpaca',
      'DUM284968',
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
    }));
    const { fixture } = await render(BotPanelShellComponent, {
      inputs: { broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
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
      inputs: { broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
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
});
