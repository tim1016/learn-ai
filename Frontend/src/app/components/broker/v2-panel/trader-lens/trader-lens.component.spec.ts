import { render, screen } from '@testing-library/angular';
import { describe, it, expect, vi } from 'vitest';
import { TraderLensComponent } from './trader-lens.component';
import type { BotPanelView, PanelProfile, ChartLiveResponse } from '../lib/broker-v2-panel.types';

// DualPaneChartComponent uses lightweight-charts — mock for unit tests.
vi.mock('lightweight-charts', () => {
  const mockTimeScale = { fitContent: vi.fn() };
  const createMockSeries = () => ({ setData: vi.fn(), applyOptions: vi.fn() });
  const createMockChart = () => ({
    addSeries: vi.fn().mockReturnValue(createMockSeries()),
    timeScale: vi.fn().mockReturnValue(mockTimeScale),
    applyOptions: vi.fn(),
    remove: vi.fn(),
  });
  return {
    createChart: vi.fn().mockImplementation(() => createMockChart()),
    CandlestickSeries: 'CandlestickSeries',
  };
});

const PROFILE: PanelProfile = {
  broker: 'alpaca',
  fee_fidelity: 'none',
  flatten_supported: false,
  live_bars_supported: false,
  stations: [],
  supported_action_ids: ['start', 'stop'],
};

const BASE_PANEL: BotPanelView = {
  strategy_instance_id: 'sid-001',
  broker: 'alpaca',
  account_id: 'DUM284968',
  symbol: 'SPY',
  mode: 'log_only',
  revision: 1,
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
  },
  clerk: {
    account_id: 'DUM284968',
    hold_active: false,
    hold_reason: 'NO_HOLD',
    hold_reason_label: 'No hold',
    hold_reason_explanation: 'No exposure hold is active.',
    hold_since_ms: null,
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
    },
  ],
};

describe('TraderLensComponent — headline', () => {
  it('shows the duty_outcome explanation as headline when present', async () => {
    const panel = {
      ...BASE_PANEL,
      health: {
        ...BASE_PANEL.health,
        duty_outcome: {
          kind: 'STOPPED_OUTCOME',
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
      screen.getByText(
        'This bot observes and decides but does not place orders (log-only). Decisions appear below.',
      ),
    ).toBeTruthy();
    // No trade table should exist
    expect(screen.queryByRole('table')).toBeNull();
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
  it('renders Stop when stop action is presented', async () => {
    await render(TraderLensComponent, {
      inputs: { panel: BASE_PANEL, profile: PROFILE, liveChart: null, histChart: null },
    });

    const btn = screen.getByRole('button', { name: 'Stop' });
    expect(btn).toBeTruthy();
  });

  it('renders Start when start action is presented', async () => {
    const panel: BotPanelView = {
      ...BASE_PANEL,
      actions: [
        {
          action_id: 'start',
          label: 'Start',
          explanation: 'Begin evaluating bars.',
          enabled: true,
          blockers: [],
          confirmation: null,
          revision: 1,
        },
      ],
    };

    await render(TraderLensComponent, {
      inputs: { panel, profile: PROFILE, liveChart: null, histChart: null },
    });

    const btn = screen.getByRole('button', { name: 'Start' });
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
