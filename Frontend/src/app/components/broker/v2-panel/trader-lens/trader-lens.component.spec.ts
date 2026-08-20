import { render, screen } from '@testing-library/angular';
import { TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { beforeEach, describe, it, expect, vi } from 'vitest';
import { TraderLensComponent } from './trader-lens.component';
import type { BotPanelView, PanelProfile, ChartLiveResponse } from '../lib/broker-v2-panel.types';
import { DUAL_PANE_CHART_FACTORY } from '../dual-pane-chart/dual-pane-chart.component';
import { MarketDataService } from '../../../../services/market-data.service';

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

// DualPaneChartComponent uses lightweight-charts — mock for unit tests.
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
      { provide: DUAL_PANE_CHART_FACTORY, useValue: chartMocks.createChart },
      {
        provide: MarketDataService,
        useValue: {
          getStockSnapshot: () => of({ success: true, snapshot: null, error: null }),
        },
      },
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

const BASE_PANEL: BotPanelView = {
  strategy_instance_id: 'sid-001',
  strategy_key: 'ema_crossover',
  strategy_label: 'Ema Crossover',
  broker: 'alpaca',
  account_id: 'DUM284968',
  symbol: 'SPY',
  mode: 'log_only',
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
    last_decision_at_ms: 1_753_800_000_000,
    decision_stale: false,
    last_bar_at_ms: 1_753_800_000_000,
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
    hold_reason_explanation: 'No exposure hold is active.',
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
  fills_today: 0,
  realized_pnl_today: 0.0,
  open_pnl: null,
  actions: [
    {
      action_id: 'stop',
      label: 'Stop',
      explanation: 'Stop evaluating bars.',
      enabled: true,
      blockers: [],
      confirmation: null,
      revision: 1,
      concurrency_token: 'test-token',
    },
  ],
  primary_action_by_lens: { trader: 'stop', operator: 'stop' },
  readiness_checks: [],
  readiness_ready_count: 0,
  readiness_blocked_count: 0,
  exposure: {},
  working_orders: [],
  recent_decisions: [],
  recent_fills: [],
};

describe('TraderLensComponent — log-only degradation', () => {
  it('renders the log-only observation panel instead of trades table', async () => {
    await render(TraderLensComponent, {
      inputs: { panel: BASE_PANEL, profile: PROFILE, liveChart: null, histChart: null },
    });

    expect(
      screen.getByText('This bot records decisions but does not place broker orders.'),
    ).toBeTruthy();
    expect(screen.queryByRole('table', { name: 'Fills today' })).toBeNull();
  });
});

describe('TraderLensComponent — trader evidence', () => {
  it('keeps strategy, Clerk, and run evidence out of the trader lens', async () => {
    const panel: BotPanelView = {
      ...BASE_PANEL,
      recent_decisions: [
        {
          seq: 7,
          bar_ref: 'bar-7',
          order_ref: null,
          outcome: 'no_action',
          reason_code: 'NO_SIGNAL',
          recorded_at_ms: 1_753_800_000_000,
          simulated: false,
        },
      ],
    };
    await render(TraderLensComponent, {
      inputs: { panel, profile: PROFILE, liveChart: null, histChart: null },
    });

    expect(screen.queryByText('Run evidence')).toBeNull();
    expect(screen.queryByText('Strategy evidence')).toBeNull();
    expect(screen.queryByText('Recent decisions')).toBeNull();
    expect(screen.queryByText(/No signal/i)).toBeNull();
    expect(screen.queryByText('Clerk evidence')).toBeNull();
  });

  it('shows P&L before the compact bot snapshot', async () => {
    const { container } = await render(TraderLensComponent, {
      inputs: { panel: BASE_PANEL, profile: PROFILE, liveChart: null, histChart: null },
    });

    const pnl = screen.getByLabelText('Profit and loss summary');
    const snapshot = screen.getByRole('table', { name: 'Bot snapshot' });
    expect(pnl).toBeTruthy();
    expect(snapshot).toBeTruthy();
    expect(
      pnl.compareDocumentPosition(snapshot) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(container.querySelector('.trader-rail app-trader-metrics')).not.toBeNull();
  });

  it('labels and styles realized-today and current-open P&L independently', async () => {
    const panel: BotPanelView = {
      ...BASE_PANEL,
      realized_pnl_today: 12.5,
      open_pnl: -4.25,
    };
    await render(TraderLensComponent, {
      inputs: { panel, profile: PROFILE, liveChart: null, histChart: null },
    });

    const realized = screen.getByLabelText('Realized profit and loss today');
    const open = screen.getByLabelText('Current open profit and loss');
    expect(realized.textContent).toContain('$12.50');
    expect(realized.querySelector('strong')?.classList.contains('positive')).toBe(true);
    expect(open.textContent).toContain('-$4.25');
    expect(open.querySelector('strong')?.classList.contains('negative')).toBe(true);
  });

  it('renders unavailable open P&L without assigning a gain or loss class', async () => {
    await render(TraderLensComponent, {
      inputs: { panel: BASE_PANEL, profile: PROFILE, liveChart: null, histChart: null },
    });

    const open = screen.getByLabelText('Current open profit and loss');
    expect(open.textContent).toContain('Mark unavailable');
    expect(open.querySelector('.positive, .negative')).toBeNull();
  });

  it('renders Unix epoch timestamps instead of treating zero as missing', async () => {
    const panel: BotPanelView = {
      ...BASE_PANEL,
      health: {
        ...BASE_PANEL.health,
        last_decision_at_ms: 0,
        last_bar_at_ms: 0,
      },
    };
    const { container } = await render(TraderLensComponent, {
      inputs: { panel, profile: PROFILE, liveChart: null, histChart: null },
    });

    expect(container.querySelectorAll('app-trader-metrics app-timestamp-display')).toHaveLength(2);
    expect(screen.queryByText('None yet')).toBeNull();
  });

  it('places fills today in the right-hand trader rail', async () => {
    const panel: BotPanelView = { ...BASE_PANEL, mode: 'trade' };
    const { container } = await render(TraderLensComponent, {
      inputs: { panel, profile: PROFILE, liveChart: null, histChart: null },
    });

    expect(
      container.querySelector('.trader-rail app-trades-today-list'),
    ).not.toBeNull();
    expect(screen.getByText('Fills today')).toBeTruthy();
  });

  it('does not claim there were no fills when the custody fold marks fill history unavailable', async () => {
    const panel: BotPanelView = {
      ...BASE_PANEL,
      mode: 'trade',
      fills_today: null,
    };
    await render(TraderLensComponent, {
      inputs: { panel, profile: PROFILE, liveChart: null, histChart: null },
    });

    expect(screen.getByText('Fill history unavailable from active custody folds.')).toBeTruthy();
    expect(screen.queryByText('No fills today.')).toBeNull();
  });
});

describe('TraderLensComponent — live fallback chip', () => {
  it('shows overlay notice message when live bars unavailable', async () => {
    const liveChart: ChartLiveResponse = {
      strategy_instance_id: 'sid-001',
      symbol: 'SPY',
      trading_date_open_ms: 1_753_800_000_000,
      trading_date_close_ms: 1_753_823_400_000,
      resolution: '1m',
      bars: [],
      fill_markers: [],
      overlay_notices: [
        {
          code: 'LIVE_UNAVAILABLE',
          message: 'Live feed unavailable — showing Polygon (delayed).',
          source: 'polygon',
        },
      ],
      as_of_ms: 1_753_800_000_000,
    };

    await render(TraderLensComponent, {
      inputs: { panel: BASE_PANEL, profile: PROFILE, liveChart, histChart: null },
    });

    expect(
      screen.getByText('Live feed unavailable — showing Polygon (delayed).'),
    ).toBeTruthy();
  });
});
