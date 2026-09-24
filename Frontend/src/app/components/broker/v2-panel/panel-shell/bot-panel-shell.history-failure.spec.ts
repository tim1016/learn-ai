/** #2202 (PRD #2201 §11.2): a focused regression spec for the Polygon
 * history failure path, kept OUT of `bot-panel-shell.component.spec.ts`
 * (already ~61 KB / 32 cases) so this file and the shard it lands in both
 * stay inside the two-minute test-gate budget.
 *
 * Before the fix, `bot-panel-shell.component.html` read
 * `histChart.value() ?? null` while `histChart` was in its error state. That
 * throws `ResourceValueError` during Angular's render pass, and the whole
 * pass is abandoned — so a click's own signal update never reaches the DOM.
 * Every case below reproduces that failure mode against the pre-fix template
 * (confirmed by hand: reverting the `hasValue()` guard on
 * `bot-panel-shell.component.html`'s `histChart` binding makes every case in
 * this file fail — the "Expand" case hangs on a DOM query that never
 * satisfies because the click's own render pass throws and is dropped, and
 * the "no new request on directory refresh" case fails because the old
 * `target()`-keyed resource re-fetches on every directory rebind).
 */
import { render, screen } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { HttpErrorResponse } from '@angular/common/http';
import { TestBed } from '@angular/core/testing';
import { afterAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { MessageService } from 'primeng/api';
import { of } from 'rxjs';
import { BotPanelShellComponent } from './bot-panel-shell.component';
import { BrokerV2PanelService } from '../lib/broker-v2-panel.service';
import { BrokersService } from '../../../../services/brokers.service';
import { MarketDataService } from '../../../../services/market-data.service';
import { fakeChartFeed } from '../../../../testing/bot-panel-fixtures';
import { FleetDirectoryService } from '../../../../fleet/fleet-directory.service';
import { DUAL_PANE_CHART_FACTORY } from '../dual-pane-chart/dual-pane-chart.component';
import { IndicatorCatalogService } from '../../../../shared/indicator-catalog/indicator-catalog.service';
import { BotChartIndicatorService } from '../dual-pane-chart/bot-chart-indicator.service';
import type { BotPanelView, BotPanelLiveSnapshot, BotRunView, PanelProfile } from '../lib/broker-v2-panel.types';
import { provideRouter } from '@angular/router';
import { provideFleetDirectory, testLane } from '../../../../fleet/fleet-directory-testing';

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

vi.mock('lightweight-charts', () => {
  const createSeriesMarkers = vi.fn().mockReturnValue({ setMarkers: vi.fn() });
  return {
    createChart: chartMocks.createChart,
    createSeriesMarkers,
    CandlestickSeries: 'CandlestickSeries',
    TickMarkType: { Year: 0, Month: 1, DayOfMonth: 2, Time: 3, TimeWithSeconds: 4 },
  };
});

class StubEventSource {
  addEventListener = vi.fn();
  close = vi.fn();

  constructor(readonly url: string) {}
}

const originalEventSource = globalThis.EventSource;
(globalThis as { EventSource?: unknown }).EventSource = StubEventSource;

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

function liveSnapshot(): BotPanelLiveSnapshot {
  return {
    stream_epoch: 'test-epoch',
    surface_version: 1,
    panel: PANEL,
    live_chart: LIVE_CHART,
  };
}

function makeRun(): BotRunView {
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
  };
}

/** A settled, *successful* zero-bar history response (#2211) — the shape
 * `build_history_chart` returns for a present-but-empty `POLYGON_API_KEY` or
 * any other Polygon fetch failure: no bars, plus zero or more canonical
 * notices on `overlay_notices`, never a rejected promise. */
function zeroBarHistoryResponse(
  overlayNotices: readonly { code: string; message: string; source: 'polygon' }[],
) {
  return {
    strategy_instance_id: 'sid-001',
    symbol: 'QQQ',
    timeframe: '1m' as const,
    from_ms: 1_753_800_000_000,
    to_ms: 1_753_823_400_000,
    bars: [],
    indicator_bars: [],
    indicator_bar_budget: 0,
    indicator_bar_budget_satisfied: true,
    fill_markers: [],
    truncated: false,
    overlay_notices: overlayNotices,
    as_of_ms: 1_753_800_000_000,
  };
}

const marketDataMock = {
  getStockSnapshot: vi.fn().mockReturnValue(of({ success: true, snapshot: null, error: null })),
};

const brokersMock = { checkSqliteRecoveryAction: vi.fn() };

/** The dual-pane chart always starts an indicator-catalog `rxResource()` on
 * construction (`supportedIndicatorResource`), independent of any Polygon
 * history state. Left unmocked it makes a real, sandbox-blocked HTTP call
 * that settles into its own error state and is unrelated to this file's
 * history-failure scenarios — every other Broker V2 spec that renders this
 * component mocks the same two services for the same reason (see
 * `dual-pane-chart.component.spec.ts`). */
const indicatorCatalogMock = {
  load: vi.fn().mockResolvedValue(undefined),
  categories: () => [],
  loading: () => false,
  failed: () => false,
};
const chartIndicatorServiceMock = {
  calculate: vi.fn().mockReturnValue(of({ symbol: 'QQQ', indicators: [] })),
  supportedIndicators: vi.fn().mockReturnValue(of({ names: [] })),
};

function makeService(historyChart: ReturnType<typeof vi.fn>) {
  return {
    getPanelProfile: vi.fn().mockResolvedValue(PROFILE),
    getLiveSnapshot: vi.fn().mockResolvedValue(liveSnapshot()),
    liveStreamUrl: vi.fn().mockReturnValue('/api/test/live-stream'),
    getCurrentRun: vi.fn().mockResolvedValue(makeRun()),
    getHistoryChart: historyChart,
  };
}

async function renderTraderPanel(historyChart: ReturnType<typeof vi.fn>) {
  const service = makeService(historyChart);
  const { fixture } = await render(BotPanelShellComponent, {
    inputs: { clerkId: 'clrk_spec', broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
    providers: [
      provideRouter([]),
      provideFleetDirectory({
        observed_at_ms: 1_757_000_000_000,
        clerks: [testLane({ clerk_id: 'clrk_spec' })],
      }),
      { provide: DUAL_PANE_CHART_FACTORY, useValue: chartMocks.createChart },
      { provide: IndicatorCatalogService, useValue: indicatorCatalogMock },
      { provide: BotChartIndicatorService, useValue: chartIndicatorServiceMock },
      { provide: MarketDataService, useValue: marketDataMock },
      { provide: BrokerV2PanelService, useValue: service },
      { provide: BrokersService, useValue: brokersMock },
      { provide: MessageService, useValue: { add: vi.fn() } },
    ],
  });
  await fixture.whenStable();
  fixture.detectChanges();
  return { fixture, service };
}

beforeEach(() => {
  chartMocks.createChart.mockClear();
});

afterAll(() => {
  globalThis.EventSource = originalEventSource;
});

describe('BotPanelShellComponent — Polygon history failure isolation (#2202)', () => {
  it('keeps the live pane and local controls immediately responsive while history is rejected', async () => {
    // Angular's default ErrorHandler logs a render-pass exception via
    // console.error; asserting none mentions ResourceValueError proves the
    // guarded read never threw (acceptance #6).
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const user = userEvent.setup();
    const historyChart = vi.fn().mockRejectedValue(
      new HttpErrorResponse({ status: 503, statusText: 'Service Unavailable' }),
    );
    const { fixture } = await renderTraderPanel(historyChart);

    // The live IBKR pane rendered — the shell did not abort its whole render
    // pass over the errored history resource.
    expect(screen.getByRole('tab', { name: 'Live' })).toBeTruthy();

    const expandButton = screen.getByRole('button', { name: 'Expand market chart' });
    await user.click(expandButton);
    await fixture.whenStable();
    fixture.detectChanges();
    expect(screen.getByRole('button', { name: 'Exit expanded market chart' })).toBeTruthy();

    const etButton = screen.getByRole('button', { name: 'ET' });
    await user.click(etButton);
    await fixture.whenStable();
    fixture.detectChanges();
    expect(etButton.getAttribute('aria-pressed')).toBe('true');

    for (const call of consoleError.mock.calls) {
      expect(call.join(' ')).not.toContain('ResourceValueError');
    }
    consoleError.mockRestore();
  });

  it('shows the Polygon-unavailable state with Retry, not an indefinite spinner, and Retry issues exactly one request', async () => {
    const user = userEvent.setup();
    const historyChart = vi.fn().mockRejectedValue(
      new HttpErrorResponse({ status: 503, statusText: 'Service Unavailable' }),
    );
    const { fixture } = await renderTraderPanel(historyChart);

    await user.click(screen.getByRole('tab', { name: '15m Delayed' }));
    await fixture.whenStable();
    fixture.detectChanges();

    expect(screen.getByText('Polygon history unavailable')).toBeTruthy();
    expect(screen.queryByText(/Loading Polygon candles/)).toBeNull();
    expect(historyChart).toHaveBeenCalledTimes(1);

    historyChart.mockResolvedValueOnce({
      strategy_instance_id: 'sid-001',
      symbol: 'QQQ',
      timeframe: '15m',
      from_ms: 1_753_800_000_000,
      to_ms: 1_753_823_400_000,
      bars: [],
      indicator_bars: [],
      indicator_bar_budget: 0,
      indicator_bar_budget_satisfied: true,
      fill_markers: [],
      truncated: false,
      as_of_ms: 1_753_800_000_000,
    });
    await user.click(screen.getByRole('button', { name: 'Retry history' }));
    await fixture.whenStable();
    fixture.detectChanges();

    expect(historyChart).toHaveBeenCalledTimes(2);
    expect(screen.queryByText('Polygon history unavailable')).toBeNull();
  });

  it('leaves no earlier bars on screen after a failed attempt (owner decision: no last-good cache)', async () => {
    const user = userEvent.setup();
    const historyChart = vi.fn().mockResolvedValueOnce({
      strategy_instance_id: 'sid-001',
      symbol: 'QQQ',
      timeframe: '1m',
      from_ms: 1_753_800_000_000,
      to_ms: 1_753_823_400_000,
      bars: [
        { start_ms: 1_753_800_000_000, end_ms: 1_753_800_060_000, open: 1, high: 2, low: 0.5, close: 1.5, volume: 10, source: 'polygon' as const },
      ],
      indicator_bars: [],
      indicator_bar_budget: 0,
      indicator_bar_budget_satisfied: true,
      fill_markers: [],
      truncated: false,
      as_of_ms: 1_753_800_000_000,
    });
    const { fixture } = await renderTraderPanel(historyChart);

    await user.click(screen.getByRole('tab', { name: '15m Delayed' }));
    await fixture.whenStable();
    fixture.detectChanges();
    expect(screen.getByText('1 candle')).toBeTruthy();

    historyChart.mockRejectedValueOnce(
      new HttpErrorResponse({ status: 503, statusText: 'Service Unavailable' }),
    );
    await user.click(screen.getByRole('button', { name: '30m' }));
    await fixture.whenStable();
    fixture.detectChanges();

    expect(screen.getByText('Polygon history unavailable')).toBeTruthy();
    expect(screen.queryByText('1 candle')).toBeNull();
  });

  it('a fleet directory generation/epoch refresh issues no new history request (FR-006)', async () => {
    const historyChart = vi.fn().mockRejectedValue(
      new HttpErrorResponse({ status: 503, statusText: 'Service Unavailable' }),
    );
    const service = makeService(historyChart);
    const directory = provideFleetDirectory({
      observed_at_ms: 1_757_000_000_000,
      clerks: [testLane({ clerk_id: 'clrk_spec' })],
    });
    const { fixture } = await render(BotPanelShellComponent, {
      inputs: { clerkId: 'clrk_spec', broker: 'alpaca', accountId: 'DUM284968', sid: 'sid-001' },
      providers: [
        provideRouter([]),
        directory,
        { provide: DUAL_PANE_CHART_FACTORY, useValue: chartMocks.createChart },
        { provide: IndicatorCatalogService, useValue: indicatorCatalogMock },
        { provide: BotChartIndicatorService, useValue: chartIndicatorServiceMock },
        { provide: MarketDataService, useValue: marketDataMock },
        { provide: BrokerV2PanelService, useValue: service },
        { provide: BrokersService, useValue: brokersMock },
        { provide: MessageService, useValue: { add: vi.fn() } },
      ],
    });
    await fixture.whenStable();
    fixture.detectChanges();
    expect(historyChart).toHaveBeenCalledTimes(1);

    directory.rebind({
      observed_at_ms: 1_757_000_100_000,
      clerks: [testLane({
        clerk_id: 'clrk_spec',
        effective_binding_generation: 99,
        routing_epoch: 7,
      })],
    });
    await TestBed.inject(FleetDirectoryService).refresh();
    fixture.detectChanges();
    await fixture.whenStable();

    expect(historyChart).toHaveBeenCalledTimes(1);
  });
});

/** #2211: since #2207, a Clerk with an empty Polygon key returns a
 * *successful* history response — zero bars plus a `polygon_api_key_missing`
 * notice on `overlay_notices` — rather than a rejected request. Before this
 * fix, `histChartFailed` stayed false for that response (it only watches
 * `histChart.error()`), so the pane fell through to "No candles in this
 * window" instead of the #2208 unavailable state: an operator was told the
 * market printed nothing when the data source was actually down, with no
 * Retry offered.
 *
 * Classification is fail-loud (`../lib/chart-history-notice.ts`): only
 * `polygon_overlay_empty` is treated as genuinely empty. Any other code on a
 * zero-bar response — including one this list has never seen, exercised
 * below with `coordinator_unavailable` (arriving separately in #2204) — is
 * unavailable.
 */
describe('BotPanelShellComponent — zero-bar history notice classification (#2211)', () => {
  it('renders the unavailable state with the notice message and a working Retry for polygon_api_key_missing', async () => {
    const user = userEvent.setup();
    const historyChart = vi.fn().mockResolvedValue(
      zeroBarHistoryResponse([{
        code: 'polygon_api_key_missing',
        message: 'Polygon history is unavailable because POLYGON_API_KEY is not configured.',
        source: 'polygon',
      }]),
    );
    const { fixture } = await renderTraderPanel(historyChart);

    await user.click(screen.getByRole('tab', { name: '15m Delayed' }));
    await fixture.whenStable();
    fixture.detectChanges();

    expect(screen.getByText('Polygon history unavailable')).toBeTruthy();
    expect(
      screen.getByText('Polygon history is unavailable because POLYGON_API_KEY is not configured.'),
    ).toBeTruthy();
    expect(screen.queryByText('No candles in this window')).toBeNull();
    expect(historyChart).toHaveBeenCalledTimes(1);

    historyChart.mockResolvedValueOnce({
      ...zeroBarHistoryResponse([]),
      bars: [
        { start_ms: 1_753_800_000_000, end_ms: 1_753_800_060_000, open: 1, high: 2, low: 0.5, close: 1.5, volume: 10, source: 'polygon' as const },
      ],
    });
    await user.click(screen.getByRole('button', { name: 'Retry history' }));
    await fixture.whenStable();
    fixture.detectChanges();

    expect(historyChart).toHaveBeenCalledTimes(2);
    expect(screen.queryByText('Polygon history unavailable')).toBeNull();
    expect(screen.getByText('1 candle')).toBeTruthy();
  });

  it('renders the unavailable state for a zero-bar response carrying an unrecognised notice code (coordinator_unavailable, #2204)', async () => {
    const user = userEvent.setup();
    const historyChart = vi.fn().mockResolvedValue(
      zeroBarHistoryResponse([{
        code: 'coordinator_unavailable',
        message: 'The fleet coordinator did not respond in time. Retry shortly.',
        source: 'polygon',
      }]),
    );
    const { fixture } = await renderTraderPanel(historyChart);

    await user.click(screen.getByRole('tab', { name: '15m Delayed' }));
    await fixture.whenStable();
    fixture.detectChanges();

    expect(screen.getByText('Polygon history unavailable')).toBeTruthy();
    expect(screen.getByText('The fleet coordinator did not respond in time. Retry shortly.')).toBeTruthy();
    expect(screen.queryByText('No candles in this window')).toBeNull();
  });

  it('still renders "No candles in this window" for a zero-bar response with no notices', async () => {
    const user = userEvent.setup();
    const historyChart = vi.fn().mockResolvedValue(zeroBarHistoryResponse([]));
    const { fixture } = await renderTraderPanel(historyChart);

    await user.click(screen.getByRole('tab', { name: '15m Delayed' }));
    await fixture.whenStable();
    fixture.detectChanges();

    expect(screen.getByText('No candles in this window')).toBeTruthy();
    expect(screen.queryByText('Polygon history unavailable')).toBeNull();
  });

  it('treats polygon_overlay_empty as genuinely empty, not unavailable — the one code in the closed empty set', async () => {
    const user = userEvent.setup();
    const historyChart = vi.fn().mockResolvedValue(
      zeroBarHistoryResponse([{
        code: 'polygon_overlay_empty',
        message: 'Polygon returned no unadjusted minute bars for missing scheduled candles.',
        source: 'polygon',
      }]),
    );
    const { fixture } = await renderTraderPanel(historyChart);

    await user.click(screen.getByRole('tab', { name: '15m Delayed' }));
    await fixture.whenStable();
    fixture.detectChanges();

    expect(screen.getByText('No candles in this window')).toBeTruthy();
    expect(screen.queryByText('Polygon history unavailable')).toBeNull();
  });
});
