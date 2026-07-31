import { render, screen } from '@testing-library/angular';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { BotPanelShellComponent } from './bot-panel-shell.component';
import { BrokerV2PanelService } from '../lib/broker-v2-panel.service';
import type { BotPanelView, PanelProfile } from '../lib/broker-v2-panel.types';

// DualPaneChartComponent -> lightweight-charts: mock for unit tests.
vi.mock('lightweight-charts', () => {
  const mockTimeScale = { fitContent: vi.fn() };
  const createMockSeries = () => ({ setData: vi.fn(), applyOptions: vi.fn() });
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
  supported_action_ids: ['start', 'stop'],
};

const PANEL: BotPanelView = {
  strategy_instance_id: 'sid-001',
  broker: 'alpaca',
  account_id: 'DUM284968',
  symbol: 'QQQ',
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
  fills_today: 0,
  realized_pnl_today: 0.0,
  open_pnl: null,
};

const mockService = {
  getPanelProfile: vi.fn().mockResolvedValue(PROFILE),
  getPanel: vi.fn().mockResolvedValue(PANEL),
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
};

describe('BotPanelShellComponent', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('shows loading state initially then renders the trader lens', async () => {
    const { fixture } = await render(BotPanelShellComponent, {
      inputs: { broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
      providers: [{ provide: BrokerV2PanelService, useValue: mockService }],
    });

    // Wait for async initial load
    await fixture.whenStable();
    fixture.detectChanges();

    // Symbol from the loaded panel should appear
    expect(screen.getByText('QQQ')).toBeTruthy();
  });

  it('shows log-only degradation panel after data loads', async () => {
    const { fixture } = await render(BotPanelShellComponent, {
      inputs: { broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
      providers: [{ provide: BrokerV2PanelService, useValue: mockService }],
    });

    await fixture.whenStable();
    fixture.detectChanges();

    expect(
      screen.getByText(
        'This bot observes and decides but does not place orders (log-only). Decisions appear below.',
      ),
    ).toBeTruthy();
  });

  it('shows an error message when panel load fails', async () => {
    mockService.getPanel.mockRejectedValueOnce(new Error('Network error'));

    const { fixture } = await render(BotPanelShellComponent, {
      inputs: { broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
      providers: [{ provide: BrokerV2PanelService, useValue: mockService }],
    });

    await fixture.whenStable();
    fixture.detectChanges();

    expect(screen.getByRole('alert')).toBeTruthy();
  });
});
