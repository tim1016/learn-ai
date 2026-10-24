import type { GridSearchDetail, GridSearchSpecRequest } from '../../../components/grid-search/grid-search.types';
import type { DataPolicy } from '../../../models/data-policy';
import type { BacktestRunDetail } from '../../../services/backtest-runs.types';
import { etDayEndMs, etMidnightMs } from '../../../shared/date/et-midnight';
import { strategyInfo } from '../../testing/fixtures';

/** The strategy every Explore spec explores, with its settings declared sweepable. */
export const EMA = 'ema_crossover_signal';

export function sweepableEma() {
  return strategyInfo({
    sweep_eligibility: { eligible: true, reason_codes: [], offending_parameters: [] },
  });
}

/** What the saved run below read: split-adjusted regular-session minute bars from the provider. */
export const SAVED_RUN_POLICY: DataPolicy = {
  source: 'polygon',
  symbol: 'QQQ',
  adjusted: true,
  session: 'regular',
  input_bars: { timespan: 'minute', multiplier: 1 },
  strategy_bars: { timespan: 'minute', multiplier: 15 },
  timestamp_policy: 'bar_close_ms_utc',
  timezone: 'America/New_York',
  provider_kind: 'live',
  fixture_id: null,
  fixture_sha256: null,
};

/** A saved Python backtest of the EMA strategy over 2026-03-02 to 2026-04-02, with its inputs on the record. */
export function savedRun(overrides: Partial<BacktestRunDetail> = {}): BacktestRunDetail {
  return {
    id: 91,
    engine: 'PYTHON',
    source: 'engine',
    requestedEngine: 'python',
    strategyName: EMA,
    symbol: 'QQQ',
    leanRunId: null,
    parameters: JSON.stringify({ fast_period: 8, slow_period: 21, symbol: 'QQQ' }),
    startDate: etMidnightMs('2026-03-02'),
    endDate: etMidnightMs('2026-04-02'),
    fillMode: 'next_bar_open',
    executedAt: 1,
    durationMs: 2,
    totalTrades: 3,
    winningTrades: 2,
    losingTrades: 1,
    winRate: 2 / 3,
    totalPnL: 120,
    initialCash: 75_000,
    commissionPerOrder: 0.35,
    finalEquity: 75_120,
    totalFees: 2.1,
    maxDrawdown: 0.02,
    sharpeRatio: 1.1,
    sortinoRatio: 1.4,
    profitFactor: 2,
    leanStatisticsJson: null,
    leanAnalysisJson: null,
    verdictJson: null,
    verdictVersion: null,
    verdictGrade: null,
    verdictSignal: null,
    equityCurve: null,
    validationAnalytics: null,
    dataPolicy: SAVED_RUN_POLICY,
    insightSummaryJson: null,
    parityGroupId: null,
    trades: [],
    tradesTruncated: false,
    closingBarSkips: [],
    metricDocumentation: [],
    notes: null,
    parityVerdicts: [],
    ...overrides,
  };
}

/** The request a Grid Search over two EMA settings was launched with. */
export function sweepRequest(overrides: Partial<GridSearchSpecRequest> = {}): GridSearchSpecRequest {
  return {
    strategy_key: EMA,
    symbol: 'SPY',
    param_ranges: {
      fast_period: { type: 'low_high_step', low: 5, high: 9, step: 2 },
      slow_period: { type: 'value_list', values: [21] },
      gap: { type: 'value_list', values: [0.2] },
    },
    start_ms: etMidnightMs('2026-01-02'),
    end_ms: etDayEndMs('2026-06-30'),
    resolution: 'minute',
    fill_mode: 'decision_minute_open',
    commission_per_order: 0.5,
    slippage_per_share: 0.01,
    initial_cash: 50_000,
    measure: 'total_return_pct',
    min_trades: 8,
    ...overrides,
  };
}

export function savedSearch(overrides: Partial<GridSearchDetail> = {}): GridSearchDetail {
  return {
    id: 'grid-search-0001',
    owner: { kind: 'user', owner_id: null, fold_index: null, phase: null },
    strategy_key: EMA,
    symbol: 'SPY',
    status: 'completed',
    job_id: 'job-1',
    created_at_ms: 1_790_000_000_000,
    finished_at_ms: 1_790_000_100_000,
    window_start_ms: etMidnightMs('2026-01-02'),
    window_end_ms: etDayEndMs('2026-06-30'),
    measure: 'total_return_pct',
    min_trades: 8,
    expected_cells: 3,
    completed_cells: 3,
    failed_cells: 0,
    leader_params_hash: 'h1',
    leader_params: { fast_period: 7, slow_period: 21 },
    incomplete: false,
    uncommitted_changes: false,
    failure_reason: null,
    request: sweepRequest(),
    receipt: {},
    resumable: false,
    resume_refusal: 'the search is complete',
    ...overrides,
  };
}
