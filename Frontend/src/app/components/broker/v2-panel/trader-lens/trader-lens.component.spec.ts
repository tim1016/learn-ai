import { render, screen } from '@testing-library/angular';
import { describe, it, expect, vi } from 'vitest';
import { TraderLensComponent } from './trader-lens.component';
import type { BotPanelView, PanelProfile, ChartLiveResponse } from '../lib/broker-v2-panel.types';

// DualPaneChartComponent uses lightweight-charts — mock for unit tests.
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
  readiness_checks: [],
  readiness_ready_count: 0,
  readiness_blocked_count: 0,
  exposure: {},
  working_orders: [],
  recent_decisions: [],
  recent_fills: [],
};

describe('TraderLensComponent — headline', () => {
  it('shows the duty_outcome explanation as headline when present', async () => {
    const panel = {
      ...BASE_PANEL,
      health: {
        ...BASE_PANEL.health,
        duty_outcome: {
          kind: 'STOPPED' as const,
          reason_code: 'STOPPED_OUTCOME',
          label: 'Stopped cleanly',
          explanation: 'Watching 1-minute bars. Last decision 10:42 — no entry.',
          recorded_at_ms: 1_753_800_000_000,
          run_id: null,
        },
      },
    };

    await render(TraderLensComponent, {
      inputs: { panel, profile: PROFILE, liveChart: null, histChart: null },
    });

    expect(
      screen.getByText('Watching 1-minute bars. Last decision 10:42 — no entry.'),
    ).toBeTruthy();
  });

  it('falls back to desired_state_label when no duty_outcome', async () => {
    await render(TraderLensComponent, {
      inputs: { panel: BASE_PANEL, profile: PROFILE, liveChart: null, histChart: null },
    });

    expect(screen.getByText('Running')).toBeTruthy();
  });
});

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
  it('keeps current run and non-trade decision evidence in the trader lens', async () => {
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

    expect(screen.getByText('Run evidence')).toBeTruthy();
    expect(screen.getByText('Strategy evidence')).toBeTruthy();
    expect(screen.getByText('Recent decisions')).toBeTruthy();
    expect(screen.getByText(/No signal/i)).toBeTruthy();
    expect(screen.getByText('Clerk evidence')).toBeTruthy();
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

  it('shows simulated fill evidence in dry-run mode', async () => {
    const panel: BotPanelView = {
      ...BASE_PANEL,
      mode: 'dry_run',
      recent_fills: [
        {
          order_ref: 'sim-1',
          symbol: 'SPY',
          side: 'buy',
          quantity: 2,
          price: 500,
          filled_at_ms: 1_753_800_000_000,
          simulated: true,
        },
      ],
    };
    await render(TraderLensComponent, {
      inputs: { panel, profile: PROFILE, liveChart: null, histChart: null },
    });

    expect(screen.getByText(/Simulated fill/)).toBeTruthy();
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

describe('TraderLensComponent — primary verb button', () => {
  it('links to the account-scoped click-to-trade ticket', async () => {
    await render(TraderLensComponent, {
      inputs: { panel: BASE_PANEL, profile: PROFILE, liveChart: null, histChart: null },
    });

    const link = screen.getByRole('link', { name: 'Manual order' });
    expect(link.getAttribute('href')).toBe('/brokers/alpaca');
  });

  it('renders Stop when stop action is presented', async () => {
    await render(TraderLensComponent, {
      inputs: { panel: BASE_PANEL, profile: PROFILE, liveChart: null, histChart: null },
    });

    const btn = screen.getByRole('button', { name: 'Stop' });
    expect(btn).toBeTruthy();
  });

  it('renders Resume when resume action is presented', async () => {
    const panel: BotPanelView = {
      ...BASE_PANEL,
      health: {
        ...BASE_PANEL.health,
        phase: 'OFF_DUTY',
        phase_label: 'Off duty',
        desired_state: 'STOPPED',
        desired_state_label: 'Stopped',
        running: false,
      },
      actions: [
        {
          action_id: 'resume',
          label: 'Resume',
          explanation: 'Begin evaluating bars.',
          enabled: true,
          blockers: [],
          confirmation: null,
          revision: 1,
          concurrency_token: 'test-token',
        },
      ],
    };

    await render(TraderLensComponent, {
      inputs: { panel, profile: PROFILE, liveChart: null, histChart: null },
    });

    const btn = screen.getByRole('button', { name: 'Resume' });
    expect(btn).toBeTruthy();
  });

  it('disables the button when actionPending is true', async () => {
    await render(TraderLensComponent, {
      inputs: {
        panel: BASE_PANEL,
        profile: PROFILE,
        liveChart: null,
        histChart: null,
        actionPending: true,
      },
    });

    const btn = screen.getByRole('button', { name: 'Stop' });
    expect((btn as HTMLButtonElement).disabled).toBe(true);
  });
});
