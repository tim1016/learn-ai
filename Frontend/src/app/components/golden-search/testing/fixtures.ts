import { etMidnightMs } from '../../../shared/date/et-midnight';
import type {
  CandidateDetail,
  CandidateKey,
  ChanceBasis,
  ChanceReadings,
  ChanceRehearsalRung,
  FinalChance,
  MeasuredChanceCarryOver,
  MeasuredChanceDrawdown,
  MeasuredChanceFitToFit,
  MeasuredChanceFitToFitDirection,
  MeasuredChanceRehearsal,
  DecisionSummary,
  EvidenceCandidate,
  EvidenceView,
  ExamOutcome,
  CurveSet,
  CurveSetKey,
  ExamFit,
  ExamView,
  RunCurve,
  RunCurves,
  RunsPage,
  StudyRun,
  ExposureState,
  ExposureView,
  Finding,
  GoldenSearchDefaults,
  GoldenSearchPreflight,
  MeasuredChanceComparison,
  MeasuredChanceProfitability,
  MeasuredConcentration,
  MeasuredTradeCharts,
  Metrics,
  PairMap,
  PairMapCell,
  PlanCharts,
  ProcedureView,
  ProcedureCharts,
  ProtocolRequest,
  QualificationDeployOffer,
  QualificationView,
  SearchCharts,
  SearchView,
  StrategyCapability,
  StudyDetail,
  StudyProgress,
  StudyState,
  TradeActivity,
  TestOverTimeCharts,
  TradeRecord,
  ValidationView,
} from '../golden-search.types';

export const DEVELOPMENT_START_MS = etMidnightMs('2024-01-01');
export const FINAL_START_MS = etMidnightMs('2026-01-01');
export const FINAL_END_MS = etMidnightMs('2026-04-01');

/** The EMA declaration as the capability endpoint describes it (owner's knob order). */
export function emaCapability(overrides: Partial<StrategyCapability> = {}): StrategyCapability {
  return {
    strategy_key: 'ema_crossover_signal',
    display_name: 'EMA Crossover Signal',
    available: true,
    reason: null,
    knobs: [
      { name: 'gap', label: 'Crossover gap', unit: 'price ($)', kind: 'decimal', domain_low: 0, domain_high: 2, quantum: 0.01, default_low: 0, default_high: 0.6, neighbor_step: 0.05, default_step: 0.05, searchable_by_default: true, warmup_dependent: false, default_value: 0.2, note: '' },
      { name: 'rsi_min', label: 'RSI lower gate', unit: 'RSI points', kind: 'decimal', domain_low: 0, domain_high: 100, quantum: 1, default_low: 30, default_high: 60, neighbor_step: 2, default_step: 1, searchable_by_default: true, warmup_dependent: false, default_value: 50, note: '' },
      { name: 'rsi_max', label: 'RSI upper gate', unit: 'RSI points', kind: 'decimal', domain_low: 0, domain_high: 100, quantum: 1, default_low: 60, default_high: 90, neighbor_step: 2, default_step: 1, searchable_by_default: true, warmup_dependent: false, default_value: 70, note: '' },
      { name: 'fast_period', label: 'Fast EMA length', unit: 'decision bars', kind: 'integer', domain_low: 2, domain_high: 30, quantum: 1, default_low: 3, default_high: 12, neighbor_step: 1, default_step: 1, searchable_by_default: true, warmup_dependent: true, default_value: 5, note: '' },
      { name: 'slow_period', label: 'Slow EMA length', unit: 'decision bars', kind: 'integer', domain_low: 3, domain_high: 40, quantum: 1, default_low: 8, default_high: 30, neighbor_step: 1, default_step: 1, searchable_by_default: true, warmup_dependent: true, default_value: 10, note: '' },
      { name: 'hold_bars', label: 'Hold time', unit: 'decision bars', kind: 'integer', domain_low: 1, domain_high: 26, quantum: 1, default_low: 2, default_high: 12, neighbor_step: 1, default_step: 1, searchable_by_default: true, warmup_dependent: false, default_value: 5, note: '' },
      { name: 'gap_bps', label: 'Crossover gap (bps)', unit: 'basis points', kind: 'decimal', domain_low: 0, domain_high: 100, quantum: 0.5, default_low: 0, default_high: 5, neighbor_step: 0.5, default_step: 0.5, searchable_by_default: false, warmup_dependent: false, default_value: 0, note: 'Both gap floors apply together: an entry must clear the price gap and this basis-point gap.' },
    ],
    fixed: [
      { label: 'RSI length', value: '14', reason: 'fixed in this program version' },
      { label: 'Decision cadence', value: '15 minutes', reason: 'fixed in this program version' },
    ],
    constraints: [
      { left: 'fast_period', op: '<', right: 'slow_period', message: 'The fast EMA must be shorter than the slow EMA.' },
      { left: 'rsi_min', op: '<', right: 'rsi_max', message: 'The lower RSI gate must be below the upper gate.' },
    ],
    default_pair_audits: [
      ['fast_period', 'slow_period'],
      ['rsi_min', 'rsi_max'],
    ],
    default_expected_trades_per_year: 50,
    importance: { low: 1, high: 10, default: 5 },
    ...overrides,
  };
}

export function unavailableCapability(): StrategyCapability {
  return {
    strategy_key: 'sma_crossover',
    display_name: 'SMA Crossover',
    available: false,
    reason: 'No Golden Search declaration has shipped for this strategy yet.',
    knobs: [],
    fixed: [],
    constraints: [],
    default_pair_audits: [],
    default_expected_trades_per_year: 50,
    importance: { low: 1, high: 10, default: 5 },
  };
}

export const INCUMBENT_PARAMS = { gap: 0.2, gap_bps: 0, rsi_min: 50, rsi_max: 70, symbol: 'SPY' } as const;

export function protocol(overrides: Partial<ProtocolRequest> = {}): ProtocolRequest {
  return {
    strategy_key: 'ema_crossover_signal',
    symbol: 'SPY',
    method: 'zoom',
    knobs: [
      { name: 'gap', mode: 'search', low: 0, high: 0.6, fixed_value: 0.2, step: 0.05, importance: 5 },
      { name: 'rsi_min', mode: 'search', low: 30, high: 60, fixed_value: 50, step: 1, importance: 5 },
      { name: 'rsi_max', mode: 'search', low: 60, high: 90, fixed_value: 70, step: 1, importance: 5 },
      { name: 'fast_period', mode: 'search', low: 3, high: 12, fixed_value: 5, step: 1, importance: 5 },
      { name: 'slow_period', mode: 'search', low: 8, high: 30, fixed_value: 10, step: 1, importance: 5 },
      { name: 'hold_bars', mode: 'search', low: 2, high: 12, fixed_value: 5, step: 1, importance: 5 },
      { name: 'gap_bps', mode: 'fixed', low: 0, high: 5, fixed_value: 0, step: null, importance: 5 },
    ],
    seed: { ...INCUMBENT_PARAMS },
    incumbent: { source: 'registry', qualification_id: null, params: { ...INCUMBENT_PARAMS } },
    policy: { objective: 'sharpe_ratio', min_trades: 30, max_drawdown_ceiling: 0.2, require_positive_net: true },
    zoom: { points: 5, refinements: 2, passes: 2 },
    development_start_ms: DEVELOPMENT_START_MS,
    development_end_ms: FINAL_START_MS,
    final_start_ms: FINAL_START_MS,
    final_end_ms: FINAL_END_MS,
    training_months: 6,
    test_months: 2,
    recent_window: true,
    pair_audits: [
      ['fast_period', 'slow_period'],
      ['rsi_min', 'rsi_max'],
    ],
    neighbor_audit: true,
    stress: [
      { key: 'slippage_1c', label: 'Extra 1¢/share slippage', slippage_add: 0.01, commission_add: 0, fill_mode: null },
      { key: 'commission_1', label: 'Extra $1 per order', slippage_add: 0, commission_add: 1, fill_mode: null },
    ],
    execution: { fill_mode: 'decision_minute_open', commission_per_order: 0, slippage_per_share: 0, initial_cash: 100000 },
    exam_min_trades: 30,
    budget_cap: 5000,
    ...overrides,
  };
}

/** A plan under an expected trade frequency: the rate set and both fixed floors absent (ADR 0074). */
export function frequencyProtocol(overrides: Partial<ProtocolRequest> = {}): ProtocolRequest {
  const base = protocol();
  return protocol({ policy: { ...base.policy, min_trades: null }, exam_min_trades: null, expected_trades_per_year: 50, ...overrides });
}

type ActivityWindow = TradeActivity['windows'][number];

function activityWindow(key: string, label: string, startIso: string, endIso: string, sessions: number, minimum: number): ActivityWindow {
  return { key, label, start_ms: etMidnightMs(startIso), end_ms: etMidnightMs(endIso), trading_sessions: sessions, minimum_trades: minimum, years: [] };
}

/** The window floors a 50-per-year plan over the fixture's intervals freezes at lock. */
export function tradeActivity(overrides: Partial<TradeActivity> = {}): TradeActivity {
  return {
    expected_trades_per_year: 50,
    windows: [
      activityWindow('development', 'Development', '2024-01-01', '2026-01-01', 502, 101),
      activityWindow('recent', 'Recent fit', '2025-07-01', '2026-01-01', 128, 26),
      activityWindow('final', 'Final test', '2026-01-01', '2026-04-01', 61, 13),
      activityWindow('training_0', 'Fold 1 training', '2024-01-01', '2024-07-01', 124, 25),
      activityWindow('training_1', 'Fold 2 training', '2024-03-01', '2024-09-01', 128, 26),
      activityWindow('forward', 'All forward tests', '2024-07-01', '2026-01-01', 378, 76),
    ],
    ...overrides,
  };
}

/** A lake that holds SPY for the whole fixture plan (its development starts on 2024-01-01), so the range picker accepts the plan's dates. */
export const STUDY_POOL = [{ symbol: 'SPY', name: 'SPDR S&P 500', firstHeld: '2023-01-03', lastHeld: '2026-09-18' }] as const;

export function defaults(overrides: Partial<GoldenSearchDefaults> = {}): GoldenSearchDefaults {
  return {
    ...protocol(),
    seed: { ...INCUMBENT_PARAMS },
    final_sessions_cut: 0,
    range_note: null,
    incumbent_label: 'Registry validated settings',
    incumbent_sentence: 'Gap $0.20 · RSI 50–70 · EMA 5/10 · hold 5 bars',
    exposure: exposure(),
    final_months: 3,
    ...overrides,
  };
}

export function exposure(overrides: Partial<ExposureView> = {}): ExposureView {
  return { state: 'not_opened', ledger_overlaps: 0, outside_activity_overlaps: 0, explanation: 'No recorded research has opened these dates.', ...overrides };
}

export function preflight(overrides: Partial<GoldenSearchPreflight> = {}): GoldenSearchPreflight {
  return {
    refusals: [],
    knob_values: [
      { name: 'gap', values: 13 },
      { name: 'rsi_min', values: 31 },
      { name: 'rsi_max', values: 31 },
      { name: 'fast_period', values: 10 },
      { name: 'slow_period', values: 23 },
      { name: 'hold_bars', values: 11 },
      { name: 'gap_bps', values: 1 },
    ],
    estimate: {
      stages: [
        { stage: 'search', label: 'Development search', max_evaluations: 205 },
        { stage: 'recent', label: 'Recent-window search', max_evaluations: 205 },
        { stage: 'validation', label: 'Test over time', max_evaluations: 1854 },
        { stage: 'evidence', label: 'Candidate evidence', max_evaluations: 83 },
        { stage: 'exam', label: 'Final test (reserved)', max_evaluations: 2 },
        { stage: 'proof', label: 'Proof (reserved)', max_evaluations: 3 },
      ],
      total_max: 2352,
      reserved_for_exam_and_proof: 5,
      budget_cap: 5000,
      serial_seconds_low: 2400,
      serial_seconds_high: 4000,
    },
    folds: [
      { fold_index: 0, train_start_ms: etMidnightMs('2024-01-01'), train_end_ms: etMidnightMs('2024-07-01'), test_start_ms: etMidnightMs('2024-07-01'), test_end_ms: etMidnightMs('2024-09-01') },
      { fold_index: 1, train_start_ms: etMidnightMs('2024-03-01'), train_end_ms: etMidnightMs('2024-09-01'), test_start_ms: etMidnightMs('2024-09-01'), test_end_ms: etMidnightMs('2024-11-01') },
    ],
    exposure: exposure(),
    run_up: { required_samples: 41, run_up_sessions: 3, data_start_ms: etMidnightMs('2023-12-27') },
    ...overrides,
  };
}

export function metrics(overrides: Partial<Metrics> = {}): Metrics {
  return { status: 'completed', total_trades: 42, net_profit: 1234.5, total_return_pct: 0.0123, sharpe_ratio: 1.18, max_drawdown_pct: 0.064, win_rate: 0.55, error: null, ...overrides };
}

export function procedureView(overrides: Partial<ProcedureView> = {}): ProcedureView {
  return {
    winner: { gap: 0.15, rsi_min: 48, rsi_max: 72, fast_period: 8, slow_period: 21, hold_bars: 4, gap_bps: 0, symbol: 'SPY' },
    winner_hash: 'w1',
    winner_metrics: metrics(),
    stop_reason: 'no_improvement',
    stop_explanation: 'A full pass moved no knob, so the search kept its last settings.',
    rounds: [
      {
        pass_index: 0,
        knob: 'fast_period',
        round_index: 0,
        low: 3,
        high: 12,
        values: [3, 5, 8, 10, 12],
        invalid: [[12, 'The fast EMA must be shorter than the slow EMA.']],
        results: [[3, 'TOO_FEW_TRADES'], [5, null], [8, null], [10, null]],
        objectives: [[3, null], [5, 0.92], [8, 1.18], [10, 1.02]],
        chosen: 8,
        moved: true,
        current_before: 5,
        quantization_limit: false,
      },
      {
        pass_index: 0,
        knob: 'slow_period',
        round_index: 0,
        low: 8,
        high: 30,
        values: [8, 13, 19, 24, 30],
        invalid: [],
        results: [[10, null], [13, null], [19, null], [24, null], [30, null]],
        objectives: [[10, 1.18], [13, 1.1], [19, 1.05], [24, 0.98], [30, 0.9]],
        chosen: 10,
        moved: false,
        current_before: 10,
        quantization_limit: false,
      },
    ],
    edge_hits: ['rsi_max'],
    evaluations: 63,
    incomplete: false,
    knob_summary: [
      { knob: 'fast_period', label: 'Fast EMA length', unit: 'decision bars', start_value: 5, retained_value: 8, moved: true, stop_reason: 'no_improvement', stop_explanation: 'No better tested move' },
      { knob: 'slow_period', label: 'Slow EMA length', unit: 'decision bars', start_value: 10, retained_value: 10, moved: false, stop_reason: 'quantization_limit', stop_explanation: 'Minimum step reached' },
    ],
    counts: { evaluated: 486, cached: 134, invalid: 18 },
    passes_completed: 2,
    window: { start_ms: DEVELOPMENT_START_MS, end_ms: FINAL_START_MS },
    ...overrides,
  };
}

/** The all-period procedure with its pair landscape around the winner. */
export function searchView(overrides: Partial<SearchView> = {}): SearchView {
  return { ...procedureView(), pair_maps: [pairMap()], pair_maps_incomplete: false, ...overrides };
}

/** A locked study's Plan charts: two folds, a frequency plan at 50 trades a year, and a lake missing one June session. */
export function planCharts(overrides: Partial<PlanCharts> = {}): PlanCharts {
  const [first, second] = preflight().folds;
  return {
    windows: [
      { key: 'run_up', label: 'Run-up before development', kind: 'run_up', start_ms: etMidnightMs('2023-12-01'), end_ms: DEVELOPMENT_START_MS, sessions: 20, minimum_trades: null },
      { key: 'development', label: 'Development', kind: 'development', start_ms: DEVELOPMENT_START_MS, end_ms: FINAL_START_MS, sessions: 502, minimum_trades: 100 },
      { key: 'training_0', label: 'Fold 1 training', kind: 'training', start_ms: first.train_start_ms, end_ms: first.train_end_ms, sessions: 250, minimum_trades: 50 },
      { key: 'test_0', label: 'Fold 1 test', kind: 'test', start_ms: first.test_start_ms, end_ms: first.test_end_ms, sessions: 63, minimum_trades: null },
      { key: 'test_1', label: 'Fold 2 test', kind: 'test', start_ms: second.test_start_ms, end_ms: second.test_end_ms, sessions: 63, minimum_trades: null },
      { key: 'final', label: 'Final test', kind: 'final', start_ms: FINAL_START_MS, end_ms: etMidnightMs('2026-07-01'), sessions: 125, minimum_trades: 25 },
    ],
    search_space: [
      { name: 'fast_period', label: 'Fast EMA length', unit: 'decision bars', searched: true, low: 3, high: 12, step: 1, values: 10, importance: 8, current: 5, domain_low: 1, domain_high: 50, low_position: 2 / 49, high_position: 11 / 49, current_position: 4 / 49, start: 5, start_position: 4 / 49 },
      { name: 'rsi_min', label: 'RSI lower gate', unit: 'RSI points', searched: false, low: 50, high: 50, step: null, values: 1, importance: null, current: 50, domain_low: 0, domain_high: 100, low_position: 0.5, high_position: 0.5, current_position: 0.5, start: 50, start_position: 0.5 },
    ],
    workload: {
      cap: 5000,
      consumed: 420,
      planned_total: 900,
      stages: [
        { stage: 'search', label: 'Search the development period', planned: 400, used: 380 },
        { stage: 'validation', label: 'Test the procedure over time', planned: 300, used: 40 },
      ],
    },
    minimums: {
      expected_trades_per_year: 50,
      windows: [{ key: 'development', label: 'Development', minimum_trades: 100, trading_years: 1.996, years: [{ year: 2024, selected_sessions: 252, year_sessions: 252 }, { year: 2025, selected_sessions: 250, year_sessions: 251 }] }],
    },
    coverage: {
      status: 'measured',
      months: [
        { month_start_ms: etMidnightMs('2024-06-01'), year: 2024, month: 6, sessions: 19, complete: 18, fetching: 0, stale: 0, failed: 0, missing: 1 },
        { month_start_ms: etMidnightMs('2024-07-01'), year: 2024, month: 7, sessions: 22, complete: 22, fetching: 0, stale: 0, failed: 0, missing: 0 },
      ],
    },
    ...overrides,
  };
}

/** One procedure's Search charts: Zoom tried the start, then fast 3 (too few trades) and 8 (kept). */
export function procedureCharts(key: ProcedureCharts['key'] = 'search', overrides: Partial<ProcedureCharts> = {}): ProcedureCharts {
  const numbers = (sharpe: number | null, trades: number, ineligibility: string | null) => ({
    sharpe_ratio: sharpe,
    net_profit: sharpe === null ? null : sharpe * 1000,
    total_return_pct: sharpe === null ? null : sharpe / 100,
    total_trades: trades,
    max_drawdown_pct: 0.06,
    ineligibility,
    objective: sharpe,
  });
  const start = { ...INCUMBENT_PARAMS };
  const kept = { ...INCUMBENT_PARAMS, fast_period: 8 };
  return {
    key,
    method: 'zoom',
    window: { start_ms: DEVELOPMENT_START_MS, end_ms: FINAL_START_MS },
    policy: { objective: 'sharpe_ratio', min_trades: 30, max_drawdown_ceiling: 0.2, require_positive_net: true },
    winner_hash: 'w',
    convergence: {
      status: 'measured',
      tried: [
        { order: 0, pass_index: null, knob: null, round_index: null, value: null, point: start, ...numbers(0.9, 40, null), best_so_far: 0.9 },
        { order: 1, pass_index: 0, knob: 'fast_period', round_index: 0, value: 3, point: { ...start, fast_period: 3 }, ...numbers(1.6, 12, 'TOO_FEW_TRADES'), best_so_far: 0.9 },
        { order: 2, pass_index: 0, knob: 'fast_period', round_index: 0, value: 8, point: kept, ...numbers(1.18, 146, null), best_so_far: 1.18 },
      ],
    },
    moves: [
      { name: 'fast_period', label: 'Fast EMA length', unit: 'decision bars', low: 3, high: 12, start: 5, retained: 8, start_position: 2 / 9, retained_position: 5 / 9, moved: true, edge_hit: false },
    ],
    profiles: [
      {
        name: 'fast_period',
        label: 'Fast EMA length',
        unit: 'decision bars',
        low: 3,
        high: 12,
        pass_index: 0,
        held: [{ name: 'gap', label: 'Crossover gap', value: 0.2 }],
        points: [
          { value: 3, ...numbers(1.6, 12, 'TOO_FEW_TRADES'), retained: false },
          { value: 5, ...numbers(0.9, 40, null), retained: false },
          { value: 8, ...numbers(1.18, 146, null), retained: true },
        ],
      },
    ],
    points: [
      { point: start, point_hash: 's', ...numbers(0.9, 40, null), winner: false },
      { point: { ...start, fast_period: 3 }, point_hash: 'f3', ...numbers(1.6, 12, 'TOO_FEW_TRADES'), winner: false },
      { point: kept, point_hash: 'w', ...numbers(1.18, 146, null), winner: true },
    ],
    ...overrides,
  };
}

export function searchCharts(procedures: ProcedureCharts[] = [procedureCharts('search'), procedureCharts('recent')]): SearchCharts {
  return { procedures };
}

/** The Test over time read for `validationView`: fold 1 completed and kept 64% of its training Sharpe, fold 2 failed. */
export function testOverTimeCharts(overrides: Partial<TestOverTimeCharts> = {}): TestOverTimeCharts {
  const validation = validationView();
  const [first, second] = preflight().folds;
  return {
    planned: false,
    in_progress: false,
    folds: [
      {
        ...first,
        winner: { ...INCUMBENT_PARAMS, fast_period: 8 },
        status: 'completed',
        failure_reason: null,
        train_sharpe: 1.4,
        test_sharpe: 0.9,
        retention: 0.9 / 1.4,
        test_return: 0.021,
        incumbent_return: 0.007,
        return_difference: 0.014,
        test_trades: 42,
        incumbent_trades: 17,
        incumbent_failure: null,
      },
      {
        ...second,
        winner: null,
        status: 'failed',
        failure_reason: "No setting met your rules in this fold's training window.",
        train_sharpe: null,
        test_sharpe: null,
        retention: null,
        test_return: null,
        incumbent_return: -0.004,
        return_difference: null,
        test_trades: null,
        incumbent_trades: 15,
        incumbent_failure: null,
      },
    ],
    linked: validation.linked,
    incumbent_linked: validation.incumbent_linked,
    retention_threshold: 0.5,
    median_retention: 0.9 / 1.4,
    test_trades_total: 42,
    forward_minimum: 30,
    below_minimum: false,
    drift: [{ name: 'fast_period', label: 'Fast EMA length', unit: 'decision bars', low: 3, high: 12, current: 5, all_period: 8, folds: [8, null] }],
    ...overrides,
  };
}

/** The same read before testing over time ran: the receipt's planned folds, nothing measured. */
export function plannedTestOverTimeCharts(): TestOverTimeCharts {
  const blank = { winner: null, status: 'planned' as const, failure_reason: null, train_sharpe: null, test_sharpe: null, retention: null, test_return: null, incumbent_return: null, return_difference: null, test_trades: null, incumbent_trades: null, incumbent_failure: null };
  return testOverTimeCharts({
    planned: true,
    folds: preflight().folds.map((fold) => ({ ...fold, ...blank })),
    linked: [],
    incumbent_linked: [],
    median_retention: null,
    test_trades_total: null,
    below_minimum: null,
    drift: [{ name: 'fast_period', label: 'Fast EMA length', unit: 'decision bars', low: 3, high: 12, current: 5, all_period: 8, folds: [null, null] }],
  });
}

export function validationView(overrides: Partial<ValidationView> = {}): ValidationView {
  const [first, second] = preflight().folds;
  return {
    folds: [
      {
        ...first,
        status: 'completed',
        winner: { ...INCUMBENT_PARAMS, fast_period: 8 },
        winner_hash: 'f0',
        train_metrics: metrics({ sharpe_ratio: 1.4 }),
        test_metrics: metrics({ sharpe_ratio: 0.9, total_return_pct: 0.021 }),
        incumbent_test_metrics: metrics({ total_trades: 17, sharpe_ratio: 0.41, total_return_pct: 0.007 }),
        failure_code: null,
        failure_reason: null,
      },
      {
        ...second,
        status: 'failed',
        winner: null,
        winner_hash: null,
        train_metrics: null,
        test_metrics: null,
        incumbent_test_metrics: metrics({ total_trades: 15, sharpe_ratio: -0.2, total_return_pct: -0.004 }),
        failure_code: 'NO_ELIGIBLE_CANDIDATE',
        failure_reason: "No setting met your rules in this fold's training window.",
      },
    ],
    verdict: {
      label: 'could not be judged',
      reason: 'Too few folds produced a defined retention.',
      successful_folds: 1,
      defined_folds: 1,
      study_retention: null,
      median_test_sharpe: 0.9,
      oos_trade_count: 42,
      based_on: 'based on 1 of 2 folds',
      retention_threshold: 0.5,
    },
    linked: [
      { fold_index: 0, test_end_ms: first.test_end_ms, linked_return: 0.021, fold_missing: false },
      { fold_index: 1, test_end_ms: second.test_end_ms, linked_return: null, fold_missing: true },
    ],
    incumbent_linked: [
      { fold_index: 0, test_end_ms: first.test_end_ms, linked_return: 0.007, fold_missing: false },
      { fold_index: 1, test_end_ms: second.test_end_ms, linked_return: 0.002972, fold_missing: false },
    ],
    summary_pills: { judged: '1 of 2 folds judged', test_trades: 42, median_retention: null },
    explanation: 'Each fold searched only its own training months.',
    incomplete: false,
    ...overrides,
  };
}

/** The fast × slow landscape around the all-period winner (8/21): fast ≥ slow cells are invalid, one run failed. */
export function pairMap(overrides: Partial<PairMap> = {}): PairMap {
  const fast = [5, 8, 10, 13, 15];
  const slow = [10, 15, 21, 26, 34];
  const returns = [
    [-0.008, 0.024, 0.046, 0.041, 0.028],
    [0.019, 0.068, 0.087, 0.081, 0.057],
    [null, 0.058, 0.082, 0.077, 0.054],
    [null, 0.032, 0.065, 0.069, 0.041],
    [null, null, 0.044, 0.052, 0.032],
  ];
  const cells: PairMapCell[] = fast.flatMap((y, row) =>
    slow.map((x, column): PairMapCell => {
      const value = returns[row][column];
      if (value === null) return { x, y, status: 'invalid', metrics: null, reason: 'The fast EMA must be shorter than the slow EMA.' };
      if (y === 13 && x === 34) return { x, y, status: 'failed', metrics: metrics({ status: 'failed', error: 'engine refused the window', total_return_pct: null, sharpe_ratio: null }), reason: null };
      return { x, y, status: 'tested', metrics: metrics({ total_return_pct: value, total_trades: 100 + row * 10 + column }), reason: null };
    }),
  );
  return { x_knob: 'slow_period', y_knob: 'fast_period', x_values: slow, y_values: fast, cells, ...overrides };
}

const ALL_PERIOD_POINT = { gap: 0.15, rsi_min: 48, rsi_max: 72, fast_period: 8, slow_period: 21, hold_bars: 4, symbol: 'SPY' } as const;
const RECENT_POINT = { gap: 0.1, rsi_min: 45, rsi_max: 70, hold_bars: 3, symbol: 'SPY' } as const;

const CHANCE_BASIS: ChanceBasis = {
  schema_version: 'golden-search-chance-comparison/v1',
  convention: 'stationary-bootstrap/v1',
  replications: 10_000,
  chunk_replications: 1_000,
  mean_block_sessions: 5,
  thresholds: { clear: 0.95 },
  objective: { measure: 'total_return_pct', label: 'total return', units: 'fraction of the starting capital' },
  seed: { inputs: ['golden-search-chance-comparison/v1', DEVELOPMENT_START_MS, FINAL_START_MS, 'total_return_pct', 'h-all', 'h-inc'], value: 1234567890 },
  source: {
    window: { start_ms: DEVELOPMENT_START_MS, end_ms: FINAL_START_MS },
    session_count: 62,
    candidate_point_hash: 'h-all',
    incumbent_point_hash: 'h-inc',
    series_digest: { candidate: 'd-all', incumbent: 'd-inc' },
    capital: 100000,
  },
};

/** A measured comparison (#3011): 96.2% at least as good, 2.7% tied, 1.1% worse, none undefined. */
export function chanceComparison(overrides: Partial<MeasuredChanceComparison> = {}): MeasuredChanceComparison {
  return {
    status: 'measured',
    label: 'clearly_as_good',
    sentence:
      'In 10,000 paired resamples of these development sessions, this fit’s total return was at least as good as the current settings’ in 96.2% of paths (9,350 strictly better, 270 tied, 380 worse). That is support under this resampling model only, not a probability of a true edge.',
    incomplete_support: false,
    bias_note: 'This fit was selected on these same development sessions.',
    counts: { better: 9350, tied: 270, worse: 380, undefined: 0 },
    shares: { better: 0.935, tied: 0.027, worse: 0.038, undefined: 0 },
    at_least_as_good: { count: 9620, share: 0.962 },
    candidate_range: { p5: 0.031, p50: 0.089, p95: 0.152, finite: 10_000 },
    difference_range: { p5: 0.019, p50: 0.037, p95: 0.066, finite: 10_000 },
    basis: CHANCE_BASIS,
    ...overrides,
  };
}

/** A measured profitability (#3011): positive on 97.4% of the fit's own resampled paths. */
export function chanceProfitability(overrides: Partial<MeasuredChanceProfitability> = {}): MeasuredChanceProfitability {
  return {
    status: 'measured',
    label: 'clearly_positive',
    sentence: 'The recorded development outcome resamples to a compounded net return positive in 97.4% of 10,000 resampled development paths (60 exactly flat, 200 negative). This is the model’s spread of what was recorded, not a forecast.',
    counts: { positive: 9740, zero: 60, negative: 200, undefined: 0 },
    shares: { positive: 0.974, zero: 0.006, negative: 0.02, undefined: 0 },
    net_return_range: {
      quantity: { measure: 'compounded_net_return', label: 'compounded net return', units: 'fraction of the starting capital' },
      p5: 0.012,
      p50: 0.081,
      p95: 0.164,
      finite: 10_000,
    },
    basis: { ...CHANCE_BASIS, schema_version: 'golden-search-chance-profitability/v1' },
    ...overrides,
  };
}

/** One fit's development chance readings (#3011-#3013); every component measured unless overridden. */
export function chanceReadings(overrides: Partial<ChanceReadings> = {}): ChanceReadings {
  return {
    comparison: chanceComparison(),
    profitability: chanceProfitability(),
    rehearsal: chanceRehearsal(),
    drawdown: chanceDrawdown(),
    ...overrides,
  };
}

/** A measured carry-over (#3014): rank 0.62 among 10,000 reproduced rehearsals unless overridden. */
export function chanceCarryOver(overrides: Partial<MeasuredChanceCarryOver> = {}): MeasuredChanceCarryOver {
  return {
    status: 'measured',
    label: 'middle',
    rank: 0.6221,
    below: 6221,
    finite: 10_000,
    undefined: 0,
    recorded_objective: 0.031,
    curve: { p: Array.from({ length: 99 }, (_, i) => -0.02 + (i / 98) * 0.09), finite: 10_000 },
    sentence: 'The recorded final total return sits inside the middle of the development rehearsals: strictly above 6,221 of the 10,000 defined rehearsal paths (62.2%), 0 undefined. This is conditional on the resampling model: a high rank is not the model proving itself right, and a low rank is not proof that bad luck is ruled out.',
    selection_note: 'This fit was chosen after the final test, from the fits that ran, and was selected by a search over the development data. Its support is unadjusted for both selections and is not a calibrated error rate or a probability of a true edge.',
    basis: {
      schema_version: 'golden-search-chance-carry-over/v1',
      rehearsal_schema: 'golden-search-chance-rehearsal/v1',
      seed: { inputs: ['golden-search-chance-rehearsal/v1', DEVELOPMENT_START_MS, FINAL_START_MS, 'total_return_pct', 'h-all', 'h-inc', 1], value: 987654321 },
      scheduled_sessions: 21,
      objective: { measure: 'total_return_pct', label: 'total return', units: 'fraction of the starting capital' },
      source_digest: 'd-all',
    },
    ...overrides,
  };
}

/** A measured fit-to-fit direction (#3029): one fit's own view of the shared draw — 68.4% at least as good unless overridden. */
export function chanceFitToFitDirection(overrides: Partial<MeasuredChanceFitToFitDirection> = {}): MeasuredChanceFitToFitDirection {
  return {
    status: 'measured',
    fit: { point_hash: 'h-all', label: 'All-period fit' },
    opponent: { point_hash: 'h-recent', label: 'Recent fit' },
    counts: { better: 6602, tied: 238, worse: 3160, undefined: 0 },
    shares: { better: 0.6602, tied: 0.0238, worse: 0.316, undefined: 0 },
    at_least_as_good: { count: 6840, share: 0.684 },
    sentence: 'In 10,000 paired resamples of the final sessions, All-period fit was at least as good as Recent fit in 68.4% of paths (6,602 better, 238 tied, 3,160 worse). Both selections were made after these results were viewed; this is not adjusted for that choice.',
    selection_note: FINAL_SELECTION_NOTE,
    basis: {
      schema_version: 'golden-search-chance-fit-to-fit/v2',
      convention: 'stationary-bootstrap/v1',
      replications: 10_000,
      mean_block_sessions: 5,
      thresholds: { clear: 0.95 },
      objective: { measure: 'total_return_pct', label: 'total return', units: 'fraction of the starting capital' },
      seed: { inputs: ['golden-search-chance-fit-to-fit/v2', FINAL_START_MS, FINAL_START_MS + 2_592_000_000, 'total_return_pct', 'h-all', 'h-recent'], value: 13579 },
      source: { window: { start_ms: FINAL_START_MS, end_ms: FINAL_START_MS + 2_592_000_000 }, session_count: 21, point_hashes: ['h-all', 'h-recent'], series_digests: ['d-all', 'd-recent'], capital: 100000 },
    },
    ...overrides,
  };
}

/** A measured fit-to-fit comparison (#3014, #3029): one shared draw, both directions stored with their identities. */
export function chanceFitToFit(overrides: Partial<MeasuredChanceFitToFit> = {}): MeasuredChanceFitToFit {
  const first = chanceFitToFitDirection();
  return {
    status: 'measured',
    pair: ['h-all', 'h-recent'],
    directions: [first, chanceFitToFitRecentDirection()],
    selection_note: FINAL_SELECTION_NOTE,
    basis: first.basis,
    ...overrides,
  };
}

/** The Recent fit's own direction of the same fixture draw: the events reversed, ties kept (34.0% inclusive). */
export function chanceFitToFitRecentDirection(): MeasuredChanceFitToFitDirection {
  return chanceFitToFitDirection({
    fit: { point_hash: 'h-recent', label: 'Recent fit' },
    opponent: { point_hash: 'h-all', label: 'All-period fit' },
    counts: { better: 3160, tied: 238, worse: 6602, undefined: 0 },
    shares: { better: 0.316, tied: 0.0238, worse: 0.6602, undefined: 0 },
    at_least_as_good: { count: 3398, share: 0.3398 },
    sentence: 'In 10,000 paired resamples of the final sessions, Recent fit was at least as good as All-period fit in 34.0% of paths (3,160 better, 238 tied, 6,602 worse). Both selections were made after these results were viewed; this is not adjusted for that choice.',
  });
}

/** The two-selection caveat the exam's final-window readings carry instead of the development bias note (#3014). */
export const FINAL_SELECTION_NOTE =
  'This fit was chosen after the final test, from the fits that ran, and was selected by a search over the development data. Its support is unadjusted for both selections and is not a calibrated error rate or a probability of a true edge.';

/** One tested fit's final chance components (#3014), its sentences worded for the final window. */
export function finalChance(overrides: Partial<FinalChance> = {}): FinalChance {
  return {
    comparison: chanceComparison({
      basis: { ...chanceComparison().basis, source: { ...chanceComparison().basis.source, window: { start_ms: FINAL_START_MS, end_ms: FINAL_START_MS + 2_592_000_000 } } },
      // The final panel's sentences name the window they resampled: the final sessions, never development ones.
      sentence:
        'In 10,000 paired resamples of these final sessions, this fit’s total return was at least as good as the current settings’ in 96.2% of paths (9,350 strictly better, 270 tied, 380 worse). That is support under this resampling model only, not a probability of a true edge.',
      bias_note: FINAL_SELECTION_NOTE,
    }),
    profitability: chanceProfitability({
      sentence:
        'The recorded final outcome resamples to a compounded net return positive in 97.4% of 10,000 resampled final paths (60 exactly flat, 200 negative). This is the model’s spread of what was recorded, not a forecast.',
    }),
    carry_over: chanceCarryOver(),
    fit_to_fit: chanceFitToFitDirection(),
    ...overrides,
  };
}

/** A measured drawdown reading (#3013): a 15.8% 95th-percentile range against a 20% ceiling unless overridden. */
export function chanceDrawdown(overrides: Partial<MeasuredChanceDrawdown> = {}): MeasuredChanceDrawdown {
  return {
    status: 'measured',
    recorded_close_only: 0.15,
    recorded_engine_per_bar: 0.164,
    percentiles: { p50: 0.092, p95: 0.158, finite: 10_000 },
    counts: { beyond_ceiling: 0, within_ceiling: 10_000, undefined: 0 },
    shares: { beyond_ceiling: 0, within_ceiling: 1, undefined: 0 },
    sentence: 'The 95th percentile of resampled session-close falls is 15.8%, within the 20% ceiling.',
    concern: false,
    basis: {
      schema_version: 'golden-search-chance-drawdown/v1',
      convention: 'stationary-bootstrap/v1',
      replications: 10_000,
      chunk_replications: 1_000,
      mean_block_sessions: 5,
      thresholds: { ceiling: 0.2 },
      horizon: { window: { start_ms: DEVELOPMENT_START_MS, end_ms: FINAL_START_MS }, scheduled_sessions: 62 },
      seed: { inputs: ['golden-search-chance-drawdown/v1', DEVELOPMENT_START_MS, FINAL_START_MS, 'h-all'], value: 424242 },
      source: { point_hash: 'h-all', series_digest: 'd-all', capital: 100000, recorded_engine_per_bar: 0.164 },
    },
    ...overrides,
  };
}

/** A measured rehearsal rung (#3012): its pass counts over 10,000 paths unless overridden. */
export function rehearsalRung(k: number, overrides: Partial<ChanceRehearsalRung> = {}): ChanceRehearsalRung {
  const joint = [6120, 7480, 8150, 8390][k - 1] ?? 6000;
  const perRule = {
    positive_net: [9210, 9540, 9680, 9750][k - 1] ?? 9000,
    drawdown: [7810, 9020, 9410, 9580][k - 1] ?? 8000,
    exits: [8840, 9610, 9800, 9880][k - 1] ?? 9000,
    objective: [7020, 8330, 8890, 9150][k - 1] ?? 7000,
  };
  const sessions = 21 * k;
  const floor = [9, 18, 27, 36][k - 1] ?? 9;
  const denominator = 10_000;
  return {
    k,
    sessions,
    trade_floor: floor,
    denominator,
    passes: { pass_joint: joint, pass_positive_net: perRule.positive_net, pass_drawdown: perRule.drawdown, pass_exits: perRule.exits, pass_objective: perRule.objective },
    undefined: { undefined_positive_net: 0, undefined_drawdown: 0, undefined_objective: 0 },
    shares: {
      joint: joint / denominator,
      positive_net: perRule.positive_net / denominator,
      drawdown: perRule.drawdown / denominator,
      exits: perRule.exits / denominator,
      objective: perRule.objective / denominator,
    },
    ...overrides,
  };
}

/** A measured rehearsal ladder (#3012): rising joint support with a 61.2% 1× rung unless overridden. */
export function chanceRehearsal(overrides: Partial<MeasuredChanceRehearsal> = {}): MeasuredChanceRehearsal {
  const rungs = [1, 2, 3, 4].map((k) => rehearsalRung(k));
  return {
    status: 'measured',
    rungs,
    active_rules: ['positive_net', 'drawdown', 'exits', 'objective'],
    guidance:
      "At the final test's own length, 61% of resamples meet every modeled rule; the limit is the objective comparison with the current settings (70%) and the session-close drawdown rule (78%). The 2× rung reaches the 80% target (75%). The 80% target is the owner's reading heuristic, not a power calculation.",
    objective_percentiles_1x: { p: Array.from({ length: 99 }, (_, i) => -0.02 + (i / 98) * 0.14), finite: 10_000 },
    basis: {
      schema_version: 'golden-search-chance-rehearsal/v1',
      convention: 'stationary-bootstrap/v1',
      replications: 10_000,
      chunk_replications: 1_000,
      mean_block_sessions: 5,
      thresholds: { rehearsal_target: 0.8 },
      objective: { measure: 'total_return_pct', label: 'total return', units: 'fraction of the starting capital' },
      rules: { max_drawdown_ceiling: 0.2, require_positive_net: true, drawdown_basis: 'session closes' },
      activity: { frequency_based: true, expected_trades_per_year: 50, final_trading_years: 0.084 },
      final_interval: { window: { start_ms: FINAL_START_MS, end_ms: FINAL_START_MS + 2_592_000_000 }, scheduled_sessions: 21 },
      source: {
        window: { start_ms: DEVELOPMENT_START_MS, end_ms: FINAL_START_MS },
        session_count: 62,
        candidate_point_hash: 'h-all',
        incumbent_point_hash: 'h-inc',
        series_digest: { candidate: 'd-all', incumbent: 'd-inc' },
        capital: 100000,
      },
      seeds: Object.fromEntries([1, 2, 3, 4].map((k) => [`${k}x`, { inputs: ['golden-search-chance-rehearsal/v1', DEVELOPMENT_START_MS, FINAL_START_MS, 'total_return_pct', 'h-all', 'h-inc', k], value: 1000 + k }])),
    },
    ...overrides,
  };
}

export function evidenceCandidate(key: CandidateKey, overrides: Partial<EvidenceCandidate> = {}): EvidenceCandidate {
  const base: Record<CandidateKey, EvidenceCandidate> = {
    all_period: {
      key: 'all_period',
      label: 'All-period fit',
      point: { ...ALL_PERIOD_POINT },
      point_hash: 'h-all',
      same_as: [],
      development_metrics: metrics({ total_return_pct: 0.087, max_drawdown_pct: 0.064, total_trades: 146, sharpe_ratio: 1.18 }),
      eligible: true,
      ineligibility: null,
      neighbors: [
        {
          knob: 'fast_period',
          one_sided: false,
          rows: [
            { value: 7, status: 'tested', metrics: metrics({ total_return_pct: 0.052 }), reason: null, step: -1, return_change: -0.035 },
            { value: 8, status: 'center', metrics: metrics({ total_return_pct: 0.087 }), reason: null, step: 0, return_change: null },
            { value: 9, status: 'invalid', metrics: null, reason: 'untested: outside the legal domain', step: 1, return_change: null },
          ],
        },
      ],
      stress: [{ scenario: 'slippage_1c', label: 'Extra 1¢/share slippage', metrics: metrics({ total_return_pct: 0.041 }), return_change: -0.046 }],
      trades_per_year: 73,
      stress_tally: { in_profit: 1, recorded: 1, scenarios: 1 },
      concentration: concentration({ without_best_month: 6800, best_trades_net_profit: 3300, without_best_trades: 5400 }),
      chance: chanceReadings(),
      guidance: { title: 'Prefer evidence that survives small changes', text: 'Less return than the recent fit, with lower drawdown and several useful neighbors.' },
      flags: [],
      params_sentence: 'Gap $0.15 · RSI 48–72 · EMA 8/21 · hold 4 bars',
      fixed_sentence: 'Normalized gap fixed at 0 bps',
      edge_hits: [],
    },
    recent: {
      key: 'recent',
      label: 'Recent fit',
      point: { ...RECENT_POINT },
      point_hash: 'h-recent',
      same_as: [],
      development_metrics: metrics({ total_return_pct: 0.116, max_drawdown_pct: 0.148, total_trades: 91, sharpe_ratio: 1.43 }),
      eligible: false,
      ineligibility: 'DRAWDOWN_ABOVE_CEILING',
      neighbors: [],
      stress: [],
      trades_per_year: 45.5,
      stress_tally: { in_profit: 0, recorded: 0, scenarios: 0 },
      concentration: concentration({ status: 'concern', net_profit: 11600, trades: 91, without_best_month: 2100, best_trades_net_profit: 12000, without_best_trades: -400 }),
      chance: chanceReadings({
        comparison: chanceComparison({
          label: 'mixed',
          sentence:
            'In 10,000 paired resamples of these development sessions, this fit’s total return was at least as good as the current settings’ in 61.4% of paths and strictly worse in 37.9% (5,900 better, 240 tied, 3,790 worse). The resampling model does not clearly separate the two settings.',
          incomplete_support: true,
          counts: { better: 5900, tied: 240, worse: 3790, undefined: 70 },
          shares: { better: 0.59, tied: 0.024, worse: 0.379, undefined: 0.007 },
          at_least_as_good: { count: 6140, share: 0.614 },
          candidate_range: { p5: -0.048, p50: 0.112, p95: 0.301, finite: 10_000 },
          difference_range: { p5: -0.071, p50: 0.021, p95: 0.118, finite: 9_930 },
        }),
        profitability: chanceProfitability({ label: 'mixed', counts: { positive: 6310, zero: 40, negative: 3650, undefined: 0 }, shares: { positive: 0.631, zero: 0.004, negative: 0.365, undefined: 0 } }),
        drawdown: chanceDrawdown({
          recorded_close_only: 0.148,
          recorded_engine_per_bar: null,
          percentiles: { p50: 0.121, p95: 0.241, finite: 9_980 },
          counts: { beyond_ceiling: 1_740, within_ceiling: 8_240, undefined: 20 },
          shares: { beyond_ceiling: 0.174, within_ceiling: 0.824, undefined: 0.002 },
          sentence: 'The 95th percentile of resampled session-close falls is 24.1%, beyond the 20% ceiling — a research concern under this model. 17.4% of resamples fall strictly beyond the ceiling and 0.2% leave the fall undefined.',
          concern: true,
        }),
        rehearsal: chanceRehearsal({
          rungs: [1, 2, 3, 4].map((k) =>
            rehearsalRung(k, {
              passes: { pass_joint: [2380, 2050, 1870, 1740][k - 1] ?? 2000, pass_positive_net: [3510, 3120, 2900, 2760][k - 1] ?? 3000, pass_drawdown: [7210, 6880, 6640, 6480][k - 1] ?? 7000, pass_exits: [4390, 4020, 3810, 3660][k - 1] ?? 4000, pass_objective: [3010, 2830, 2710, 2620][k - 1] ?? 3000 },
              shares: {
                joint: ([2380, 2050, 1870, 1740][k - 1] ?? 2000) / 10_000,
                positive_net: ([3510, 3120, 2900, 2760][k - 1] ?? 3000) / 10_000,
                drawdown: ([7210, 6880, 6640, 6480][k - 1] ?? 7000) / 10_000,
                exits: ([4390, 4020, 3810, 3660][k - 1] ?? 4000) / 10_000,
                objective: ([3010, 2830, 2710, 2620][k - 1] ?? 3000) / 10_000,
              },
            }),
          ),
          guidance:
            "At the final test's own length, 24% of resamples meet every modeled rule; the limit is the objective comparison with the current settings (30%) and the positive-net rule (35%). No rung reaches the 80% target; the highest is 24% at 1×. Support does not rise with length on this ladder — a longer rehearsal of the same development outcome is not a better one. The 80% target is the owner's reading heuristic, not a power calculation.",
        }),
      }),
      guidance: { title: 'The extra return comes with a warning', text: 'The largest development return also exceeds your drawdown ceiling.' },
      flags: [{ code: 'DRAWDOWN_ABOVE_CEILING', text: 'Above 12% limit' }],
      params_sentence: 'Gap $0.10 · RSI 45–70 · EMA 5/10 · hold 3 bars',
      fixed_sentence: 'Normalized gap fixed at 0 bps',
      edge_hits: [],
    },
    incumbent: {
      key: 'incumbent',
      label: 'Current settings',
      point: { ...INCUMBENT_PARAMS },
      point_hash: 'h-inc',
      same_as: [],
      development_metrics: metrics({ total_return_pct: 0.052, max_drawdown_pct: 0.089, total_trades: 158, sharpe_ratio: 0.82 }),
      eligible: true,
      ineligibility: null,
      neighbors: [],
      stress: [],
      trades_per_year: 79,
      stress_tally: { in_profit: 0, recorded: 0, scenarios: 0 },
      // Evidence recorded before the evidence stage measured concentration.
      concentration: { status: 'missing', reason: 'Not measured for this study: its evidence was recorded before concentration was measured.' },
      // The current settings are the baseline every fit is compared against, never with themselves.
      chance: {
        comparison: {
          status: 'baseline',
          sentence: 'The current settings are the baseline every fit is compared against. They are not compared with themselves.',
          basis: { ...CHANCE_BASIS, source: { window: { start_ms: DEVELOPMENT_START_MS, end_ms: FINAL_START_MS }, session_count: 62 } } satisfies ChanceBasis,
        },
        profitability: { status: 'missing', code: 'not_measured', reason: 'The current settings’ own profitability is not measured; every fit’s readings stand against them.' },
        rehearsal: { status: 'missing', code: 'baseline', reason: 'The current settings are the baseline; a rehearsal compares a fit against them, and there is nothing to rehearse.' },
        drawdown: chanceDrawdown({ recorded_close_only: 0.102, recorded_engine_per_bar: 0.098, percentiles: { p50: 0.064, p95: 0.119, finite: 10_000 } }),
      },
      guidance: { title: 'No change can be the best decision', text: 'Keeping the incumbent is a complete research decision.' },
      flags: [],
      params_sentence: 'Gap $0.20 · RSI 50–70 · EMA 5/10 · hold 5 bars',
      fixed_sentence: 'Normalized gap fixed at 0 bps',
      edge_hits: [],
    },
  };
  return { ...base[key], ...overrides };
}

/** A measured result (#2815): 8,700 net over 146 development trades unless overridden; its best month is November 2025. */
export function concentration(overrides: Partial<MeasuredConcentration> = {}): MeasuredConcentration {
  const trades = overrides.trades ?? 146;
  return {
    status: 'meets',
    net_profit: 8700,
    trades,
    best_month: { month_start_ms: etMidnightMs('2025-11-01'), net_profit: 1900 },
    without_best_month: 6800,
    best_trades: Array.from({ length: Math.ceil(trades / 20) }, (_, i) => ({ entry_ms: etMidnightMs('2025-12-23') + 15 * 3600_000 - i * 86_400_000, exit_ms: etMidnightMs('2025-12-23') + 16 * 3600_000 - i * 86_400_000, net_profit: 600 - i * 50 })),
    best_trades_net_profit: 3300,
    without_best_trades: 5400,
    ...overrides,
  };
}

export function evidenceView(overrides: Partial<EvidenceView> = {}): EvidenceView {
  return {
    candidates: [evidenceCandidate('incumbent'), evidenceCandidate('all_period'), evidenceCandidate('recent')],
    fits_to_test: [
      { key: 'all_period', same_as: [], label: 'All-period fit' },
      { key: 'recent', same_as: [], label: 'Recent fit' },
    ],
    pair_maps: [pairMap()],
    recommendation: {
      headline: 'The recent fit earns more in this replay, but nearby settings lose money. Inspect that sensitivity before using your final test.',
      findings: [{ code: 'RECENT_DIFFERS_FROM_ALL_PERIOD', text: 'The recent fit chose different EMA lengths than the all-period fit.' }],
    },
    scope: { window: { start_ms: DEVELOPMENT_START_MS, end_ms: FINAL_START_MS }, capital: 100000, costs: { fill_mode: 'decision_minute_open', commission_per_order: 0, slippage_per_share: 0 } },
    incomplete: false,
    ...overrides,
  };
}

/** The server's reason for trades that do not add up to their run's net profit. */
export const UNRECONCILED = 'Its trades add up to $100.00 after commission, but the run’s net profit is $250.00, so the trades do not account for the whole result.';

/** Two development trades at no commission (+$243 on Tuesday, then -$92 closed by the window's end on Wednesday) and what the server makes of them. */
export function tradeCharts(trades: Partial<TradeRecord>[] = [{}, {}]): MeasuredTradeCharts {
  const base: TradeRecord[] = [
    { entry_ms: etMidnightMs('2025-12-23') + 15 * 3600_000, exit_ms: etMidnightMs('2025-12-23') + 16 * 3600_000, entry_price: 590.1, exit_price: 592.5, quantity: 77, pnl: 243, net_profit: 243, running_net_profit: 243, bars_held: 4, entry_rsi: 58.2, exit_kind: 'strategy', exit_reason: 'Exited by the strategy' },
    { entry_ms: etMidnightMs('2025-12-24') + 10 * 3600_000, exit_ms: etMidnightMs('2025-12-24') + 12 * 3600_000, entry_price: 594, exit_price: 592.8, quantity: 77, pnl: -92, net_profit: -92, running_net_profit: 151, bars_held: 8, entry_rsi: 54, exit_kind: 'window_end', exit_reason: 'Closed at the end of the tested window' },
  ];
  return {
    status: 'measured',
    bar_span_ms: 15 * 60_000,
    hold_reason: null,
    trades: trades.map((overrides, i) => ({ ...base[i], ...overrides })),
    histogram: {
      bin_width: 200,
      rule: 'freedman_diaconis',
      bins: [
        { low: -200, high: 0, trades: 1, wins: 0, losses: 1 },
        { low: 0, high: 200, trades: 0, wins: 0, losses: 0 },
        { low: 200, high: 400, trades: 1, wins: 1, losses: 0 },
      ],
    },
    entry_rsi: {
      status: 'measured',
      gate_low: 50,
      gate_high: 70,
      bands: [
        { low: 50, high: 55, trades: 1, mean_net_profit: -92 },
        { low: 55, high: 60, trades: 1, mean_net_profit: 243 },
        { low: 60, high: 65, trades: 0, mean_net_profit: null },
        { low: 65, high: 70, trades: 0, mean_net_profit: null },
      ],
      unbanded: 0,
    },
    entry_times: {
      weekdays: ['Mon', 'Tue', 'Wed', 'Thu', 'Fri'],
      half_hours: ['09:30', '10:00', '10:30', '11:00', '11:30', '12:00', '12:30', '13:00', '13:30', '14:00', '14:30', '15:00', '15:30'],
      min_trades: 5,
      cells: [
        { weekday: 1, half_hour: 11, trades: 1, mean_net_profit: 243, total_net_profit: 243, too_few: true },
        { weekday: 2, half_hour: 1, trades: 1, mean_net_profit: -92, total_net_profit: -92, too_few: true },
      ],
    },
  };
}

/** A final-test run: +1.8% for the all-period fit, +1.2% for the recent fit, +0.9% for the current settings, with its measures beside development. */
export function finalRun(key: CandidateKey): NonNullable<CandidateDetail['exam']> {
  const end = { all_period: 0.018, recent: 0.012, incumbent: 0.009 }[key];
  const sessions = [etMidnightMs('2026-01-02'), etMidnightMs('2026-02-02'), etMidnightMs('2026-03-31')];
  return {
    window: { start_ms: FINAL_START_MS, end_ms: etMidnightMs('2026-04-01') },
    metrics: metrics({ total_return_pct: end, sharpe_ratio: key === 'incumbent' ? 0.54 : 0.91, total_trades: key === 'incumbent' ? 39 : 36 }),
    // Up half the way, back to three tenths, then up to the end: the fall is the dip from the first peak.
    cumulative_return: sessions.map((ms, i) => ({ ms, value: [end * 0.5, end * 0.3, end][i] })),
    daily_equity: [],
    drawdown: sessions.map((ms, i) => ({ ms, drawdown: i === 1 ? (1 + end * 0.3) / (1 + end * 0.5) - 1 : 0 })),
    monthly: [
      { month_start_ms: etMidnightMs('2026-01-01'), year: 2026, month: 1, net_profit: end * 40_000, return_fraction: end * 0.4, trades: 12 },
      { month_start_ms: etMidnightMs('2026-02-01'), year: 2026, month: 2, net_profit: end * 60_000, return_fraction: end * 0.6, trades: 13 },
    ],
    concentration_curve: null,
    trade_charts: null,
    comparison: [
      { key: 'annualized_return', label: 'Annualized return', development: 0.042, final: key === 'incumbent' ? 0.037 : 0.075, change: key === 'incumbent' ? -0.005 : 0.033 },
      { key: 'sharpe_ratio', label: 'Sharpe', development: 1.18, final: key === 'incumbent' ? 0.54 : 0.91, change: key === 'incumbent' ? -0.64 : -0.27 },
      { key: 'max_drawdown_pct', label: 'Worst fall', development: 0.064, final: 0.031, change: -0.033 },
      { key: 'trades_per_year', label: 'Trades a trading year', development: 73, final: 146, change: 73 },
    ],
  };
}

export function candidateDetail(key: CandidateKey, overrides: Partial<CandidateDetail> = {}): CandidateDetail {
  const end = { all_period: 0.087, recent: 0.116, incumbent: 0.052 }[key];
  const days = [etMidnightMs('2024-01-02'), etMidnightMs('2024-06-03'), etMidnightMs('2025-12-31')];
  return {
    candidate_key: key,
    point: evidenceCandidate(key).point,
    development: {
      window: { start_ms: DEVELOPMENT_START_MS, end_ms: FINAL_START_MS },
      metrics: evidenceCandidate(key).development_metrics ?? metrics(),
      cumulative_return: [
        { ms: days[0], value: 0 },
        { ms: days[1], value: end / 2 },
        { ms: days[2], value: end },
      ],
      daily_equity: [],
      comparison: null,
      drawdown: [
        { ms: days[0], drawdown: 0 },
        { ms: days[1], drawdown: -end / 4 },
        { ms: days[2], drawdown: 0 },
      ],
      monthly: [
        { month_start_ms: etMidnightMs('2025-11-01'), year: 2025, month: 11, net_profit: 1100, return_fraction: 0.011, trades: 12 },
        { month_start_ms: etMidnightMs('2025-12-01'), year: 2025, month: 12, net_profit: -700, return_fraction: -0.007, trades: 9 },
      ],
      trade_charts: key === 'incumbent' ? { status: 'missing', reason: UNRECONCILED } : tradeCharts(),
      // The two trades of the trade charts, best first, at no commission: 243 then 151 of 151.
      concentration_curve: {
        points: [
          { trades: 0, share_of_trades: 0, share_of_profit: 0, net_profit: 0 },
          { trades: 1, share_of_trades: 0.5, share_of_profit: 243 / 151, net_profit: 243 },
          { trades: 2, share_of_trades: 1, share_of_profit: 1, net_profit: 151 },
        ],
        best_count: 1,
        reason: null,
      },
    },
    exam: null,
    ...overrides,
  };
}

/** The server's weakness copy (`guidance.weakness_items`) for an outcome short of the rules, beside a failed rule. */
const EXAM_WEAKNESS: Readonly<Record<Exclude<ExamOutcome, 'meets_rules' | 'does_not_meet_rules'>, string>> = {
  not_enough_evidence: 'not enough final-test evidence',
  could_not_evaluate: 'a final test that could not be evaluated',
};
/** The same for a test interval that can only be exploratory. */
const EXPOSURE_WEAKNESS: Readonly<Record<ExposureState, string>> = {
  not_opened: 'an exploratory, not confirmatory, final test',
  previously_used: 'the previously used test interval',
  history_unknown: 'the unknown history of this test interval',
};

type ExamClaim = Pick<ExamView, 'claim' | 'exposure_state'>;

/** What the server names as weak about a fit: nothing before an outcome, or when the evidence meets the rules. */
function weaknessOf(fit: Omit<ExamFit, 'weakness'>, exam: ExamClaim): Finding[] {
  if (fit.outcome === null) return [];
  const found: Finding[] = [];
  if (fit.outcome === 'does_not_meet_rules') {
    const failed = fit.checks.filter((check) => check.status === 'fail').map((check) => check.label.toLowerCase());
    found.push({ code: 'EXAM_DOES_NOT_MEET_RULES', text: failed.length > 0 ? `failing the stated rules (${failed.join(', ')})` : 'failing the stated rules' });
  } else if (fit.outcome !== 'meets_rules') {
    found.push({ code: `EXAM_${fit.outcome.toUpperCase()}`, text: EXAM_WEAKNESS[fit.outcome] });
  }
  if (exam.claim !== 'confirmatory') found.push({ code: `EXPOSURE_${exam.exposure_state.toUpperCase()}`, text: EXPOSURE_WEAKNESS[exam.exposure_state] });
  return found;
}

const FIT_DEFAULTS: Readonly<Record<'all_period' | 'recent', Omit<ExamFit, 'weakness'>>> = {
  all_period: {
    key: 'all_period',
    same_as: [],
    label: 'All-period fit',
    point: { ...ALL_PERIOD_POINT },
    outcome: 'meets_rules',
    checks: [
      { code: 'NET_POSITIVE', label: 'Profit after stated costs', status: 'pass', detail: 'Net profit $1,840.' },
      { code: 'DRAWDOWN_WITHIN', label: 'Worst drawdown within the ceiling', status: 'pass', detail: '3.1% against 20%.' },
      { code: 'SAMPLE_FLOOR', label: 'Enough trades', status: 'pass', detail: '36 trades against 30.' },
      { code: 'BEATS_INCUMBENT', label: 'At least the incumbent', status: 'pass', detail: 'Sharpe 0.91 against 0.54.' },
    ],
    retention: 0.61,
    metrics: metrics({ total_return_pct: 0.018, max_drawdown_pct: 0.031, total_trades: 36, sharpe_ratio: 0.91 }),
    chance: finalChance(),
  },
  recent: {
    key: 'recent',
    same_as: [],
    label: 'Recent fit',
    point: { ...RECENT_POINT },
    outcome: 'meets_rules',
    checks: [
      { code: 'NET_POSITIVE', label: 'Profit after stated costs', status: 'pass', detail: 'Net profit $1,210.' },
      { code: 'DRAWDOWN_WITHIN', label: 'Worst drawdown within the ceiling', status: 'pass', detail: '5.2% against 20%.' },
      { code: 'SAMPLE_FLOOR', label: 'Enough trades', status: 'pass', detail: '33 trades against 30.' },
      { code: 'BEATS_INCUMBENT', label: 'At least the incumbent', status: 'pass', detail: 'Sharpe 0.66 against 0.54.' },
    ],
    retention: 0.46,
    metrics: metrics({ total_return_pct: 0.012, max_drawdown_pct: 0.052, total_trades: 33, sharpe_ratio: 0.66 }),
    chance: finalChance({
      comparison: chanceComparison({
        label: 'mixed',
        sentence:
          'In 10,000 paired resamples of these final sessions, this fit’s total return was at least as good as the current settings’ in 58.2% of paths and strictly worse in 41.5% (4,120 better, 1,700 tied, 4,150 worse). The resampling model does not clearly separate the two settings.',
        counts: { better: 4120, tied: 1700, worse: 4150, undefined: 30 },
        shares: { better: 0.412, tied: 0.17, worse: 0.415, undefined: 0.003 },
        at_least_as_good: { count: 5820, share: 0.582 },
      }),
      carry_over: chanceCarryOver({ rank: 0.0412, below: 412, label: 'below_most', sentence: 'The recorded final total return sits below most development rehearsals: strictly above 412 of the 10,000 defined rehearsal paths (4.1%), 0 undefined. This is conditional on the resampling model: a high rank is not the model proving itself right, and a low rank is not proof that bad luck is ruled out.' }),
    }),
  },
};

/** One fit of a final test; its `weakness` follows its outcome and the test's claim as the server's does, unless an override names it. */
export function examFit(key: 'all_period' | 'recent' = 'all_period', overrides: Partial<ExamFit> = {}, exam: ExamClaim = { claim: 'confirmatory', exposure_state: 'not_opened' }): ExamFit {
  const fit = { ...FIT_DEFAULTS[key], ...overrides };
  return { ...fit, weakness: overrides.weakness ?? weaknessOf(fit, exam) };
}

export const UNDECIDED_NOTE = '2 fits ran on the final test. The one you approve is recorded as chosen after the final test, from 2 fits.';
export const CHOSEN_NOTE = 'Chosen after the final test, from 2 fits.';

/** A final test of both fits, not yet decided, unless `fits` names others; `allPeriod` overrides the all-period fit. */
export function examView(overrides: Partial<ExamView> = {}, allPeriod: Partial<ExamFit> = {}): ExamView {
  const claim: ExamClaim = { claim: overrides.claim ?? 'confirmatory', exposure_state: overrides.exposure_state ?? 'not_opened' };
  const fits = overrides.fits ?? [examFit('all_period', allPeriod, claim), examFit('recent', {}, claim)];
  const chosen = overrides.chosen_key ?? null;
  return {
    window: { start_ms: FINAL_START_MS, end_ms: FINAL_END_MS },
    ...claim,
    incumbent_metrics: metrics({ total_return_pct: 0.009, max_drawdown_pct: 0.042, total_trades: 39, sharpe_ratio: 0.54 }),
    chance: { fit_to_fit: chanceFitToFit() },
    pick: 'after_test',
    chosen_key: chosen,
    selection_note: fits.length < 2 ? null : chosen === null ? UNDECIDED_NOTE : CHOSEN_NOTE,
    positive_net_required: true,
    ...overrides,
    fits,
  };
}

/** A final test still running: every fit opened, none scored. */
function examRunning(): ExamView {
  const unscored = { outcome: null, checks: [], retention: null, metrics: null };
  return examView({ fits: [examFit('all_period', unscored), examFit('recent', unscored)], incumbent_metrics: null });
}

export function qualificationView(overrides: Partial<QualificationView> = {}): QualificationView {
  return {
    status: 'ready',
    qualification_id: 'gq-0001-aaaa-bbbb',
    failure_reason: null,
    deploy: { program_key: 'ema_crossover_signal', symbol: 'SPY', parameters: { gap: 0.15, rsi_min: 48, rsi_max: 72, fast_period: 8, slow_period: 21, hold_bars: 4 }, program_version: 'ema-crossover-signal/v3' },
    ...overrides,
  };
}

export function deployOffer(overrides: Partial<QualificationDeployOffer> = {}): QualificationDeployOffer {
  return {
    qualification_id: 'gq-0001-aaaa-bbbb',
    program_key: 'ema_crossover_signal',
    program_version: 'ema-crossover-signal/v3',
    symbol: 'QQQ',
    parameters: { gap: 0.15, rsi_min: 48, rsi_max: 72, fast_period: 8, slow_period: 21 },
    status: 'ready',
    explanation: 'Approved in Golden Search study study-00; its proof matches the running program.',
    is_default: true,
    research: { claim: 'confirmatory', exam_outcome: 'meets_rules', exposure_state: 'not_opened', research_override: false, validation_verdict_label: 'still worked', weakness: [] },
    ...overrides,
  };
}

/** Why the fixture's approval failed; the server repeats it as the study's failure reason and its guidance detail. */
export const QUALIFICATION_FAILURE = 'The restored replay did not match the lake replay (TRACE_MISMATCH).';

const GUIDANCE: Readonly<Record<StudyState, { headline: string; detail: string }>> = {
  locked: { headline: 'Start the search when the plan is right', detail: 'The plan is frozen; the search runs on development data only.' },
  search_running: { headline: 'The search is running', detail: 'Results appear when the stage finishes.' },
  awaiting_validation: { headline: 'Check whether the search procedure holds up over time', detail: 'Each fold repeats the procedure on its own past.' },
  validation_running: { headline: 'Testing the procedure over time', detail: 'Folds run one after another.' },
  awaiting_candidate: { headline: 'Compare the candidates', detail: 'Open the final test for every fit, or keep the current settings.' },
  exam_running: { headline: 'The final test is running', detail: 'Every fit and the incumbent run on the held-back dates.' },
  awaiting_review: { headline: 'Decide', detail: 'Approve a fit or keep the current settings.' },
  qualification_pending: { headline: 'Building the proof', detail: 'Qualification is running.' },
  approved: { headline: 'Golden configuration ready in Deploy', detail: 'Use it in Deploy.' },
  qualification_failed: { headline: 'Qualification failed · current default unchanged', detail: QUALIFICATION_FAILURE },
  retained: { headline: 'Current settings retained', detail: 'The study stays in history.' },
  closed: { headline: 'Study closed', detail: 'The study stays in history.' },
};

/** The stage each running state reports progress for. */
const PROGRESS_STAGE: Partial<Readonly<Record<StudyState, StudyProgress['stage']>>> = {
  search_running: 'search',
  validation_running: 'validation',
  exam_running: 'exam',
  qualification_pending: 'qualification',
};

/** States reached only after the final test was opened. */
const AFTER_EXAM: readonly StudyState[] = ['exam_running', 'awaiting_review', 'qualification_pending', 'approved', 'qualification_failed'];
/** States reached only once an approval named a fit. */
const AFTER_PICK: readonly StudyState[] = ['qualification_pending', 'approved', 'qualification_failed'];

function progressFor(state: StudyState): StudyProgress | null {
  const stage = PROGRESS_STAGE[state];
  return stage === undefined ? null : { stage, completed: 120, total_max: 410 };
}

function qualificationFor(state: StudyState): QualificationView | null {
  if (state === 'approved') return qualificationView();
  if (state === 'qualification_pending') return qualificationView({ status: 'pending', qualification_id: null, deploy: null });
  if (state === 'qualification_failed') {
    return qualificationView({ status: 'failed', qualification_id: null, deploy: null, failure_reason: QUALIFICATION_FAILURE });
  }
  return null;
}

/** A candidate's decision summary as the server classifies it: meets, a concern, and evidence that was not recorded. */
export function decisionSummary(key: CandidateKey, rows: DecisionSummary['rows'] | null = null): DecisionSummary {
  return {
    candidate_key: key,
    rows: rows ?? [
      { key: 'development_activity', label: 'Development activity', status: 'meets', text: '42 trades over the development period; its minimum is 30.', link: { kind: 'tab', target: 'trades' } },
      { key: 'test_over_time', label: 'Test over time', status: 'meets', text: 'The search procedure still worked: median out-of-sample Sharpe is positive. 9 of 9 scheduled folds completed.', link: { kind: 'step', target: 'test' } },
      { key: 'neighbors', label: 'Neighbor sensitivity', status: 'concern', text: 'A one-step change in hold_bars loses money.', link: { kind: 'chart', target: 'neighbor-tornado' } },
      { key: 'stress', label: 'Cost stresses', status: 'meets', text: 'Every stressed run still makes money.', link: { kind: 'chart', target: 'cost-stress' } },
      { key: 'concentration', label: 'Concentration', status: 'meets', text: 'Still profitable without its best month ($6,800.00) or its best 8 trades ($5,400.00).', link: { kind: 'chart', target: 'without-best' } },
      { key: 'final_exposure', label: 'Final-test exposure', status: 'meets', text: 'No recorded research has used the final interval, so its one look counts as confirmatory.', link: { kind: 'step', target: 'decision' } },
    ],
  };
}

export function studyDetail(state: StudyState, overrides: Partial<StudyDetail> = {}): StudyDetail {
  const reachedSearch = !['locked', 'search_running'].includes(state);
  const reachedValidation = reachedSearch && !['awaiting_validation', 'validation_running'].includes(state);
  const examOpened = AFTER_EXAM.includes(state);
  const examJudged = examOpened && state !== 'exam_running';
  const picked: CandidateKey | null = AFTER_PICK.includes(state) ? 'all_period' : null;
  return {
    id: 'study-0001-aaaa',
    parent_study_id: null,
    strategy_key: 'ema_crossover_signal',
    symbol: 'SPY',
    state,
    presented_status: state.endsWith('_running') || state === 'qualification_pending' ? 'running' : 'idle',
    revision: 3,
    created_at_ms: etMidnightMs('2026-09-30'),
    updated_at_ms: etMidnightMs('2026-09-30'),
    protocol_hash: 'abcdef0123456789',
    method: 'zoom',
    consumed_evaluations: 420,
    budget_cap: 5000,
    cache_hits: 37,
    invalid_points: 6,
    incomplete: false,
    failure_reason: state === 'qualification_failed' ? QUALIFICATION_FAILURE : null,
    hidden: false,
    run_to_compare: false,
    decision_summaries: reachedValidation ? [decisionSummary('all_period'), decisionSummary('recent'), decisionSummary('incumbent')] : [],
    exposure_claim: examOpened ? 'confirmatory' : null,
    exam_outcome: examJudged ? 'meets_rules' : null,
    qualification_id: state === 'approved' ? 'gq-0001-aaaa-bbbb' : null,
    protocol: protocol(),
    receipt: {
      data_start_ms: etMidnightMs('2023-12-27'),
      development_start_ms: DEVELOPMENT_START_MS,
      development_end_ms: FINAL_START_MS,
      final_start_ms: FINAL_START_MS,
      final_end_ms: FINAL_END_MS,
      run_up_sessions: 3,
      snapshot_digest: 'snap0123456789abcdef',
      code: { git_revision: '7e43a777aaaabbbb', tree_state: 'clean' },
      program_version: 'ema-crossover-signal/v3',
    },
    permitted_actions: permittedFor(state),
    action_refusals: {},
    guidance: GUIDANCE[state],
    progress: progressFor(state),
    dispatch: null,
    results: {
      search: reachedSearch ? searchView() : null,
      recent: reachedSearch ? procedureView({ winner_hash: 'r1', stop_reason: 'pass_limit', stop_explanation: 'The pass limit was reached while knobs still moved.', edge_hits: [] }) : null,
      validation: reachedValidation ? validationView() : null,
      evidence: reachedValidation ? evidenceView() : null,
      exam: examJudged ? examView({ chosen_key: picked }) : examOpened ? examRunning() : null,
      qualification: qualificationFor(state),
    },
    decision: state === 'approved' ? { kind: 'approve', note: 'Accept this exact configuration after reviewing the stated limitations.', at_ms: etMidnightMs('2026-09-30') } : null,
    candidate_key: picked,
    exam_locked: examOpened,
    exposure_preview: state === 'awaiting_candidate' ? exposure() : null,
    scope: {
      development_label_start_ms: DEVELOPMENT_START_MS,
      development_end_ms: FINAL_START_MS,
      final_start_ms: FINAL_START_MS,
      final_end_ms: FINAL_END_MS,
      final_state: examOpened ? 'opened_once' : 'locked',
      capital: 100000,
      costs_sentence: 'No commission · no slippage · fills at the decision minute open',
      data_source: 'Historical research: Polygon, split adjusted, regular sessions',
    },
    ...overrides,
  };
}

/** What the server permits in each state, as the lifecycle table in #2696 lists it. */
function permittedFor(state: StudyState): StudyDetail['permitted_actions'] {
  switch (state) {
    case 'locked':
      return ['continue', 'run_research', 'revise', 'close'];
    case 'awaiting_validation':
      return ['continue', 'run_research', 'retain', 'revise', 'close'];
    case 'awaiting_candidate':
      return ['open_exam', 'retain', 'revise', 'close'];
    case 'awaiting_review':
    case 'qualification_failed':
      return ['approve', 'retain', 'revise'];
    case 'search_running':
    case 'validation_running':
    case 'exam_running':
    case 'qualification_pending':
      return ['cancel'];
    default:
      return ['revise'];
  }
}

/** One run as the All runs page lists it: an all-period search point with its curve, unless overridden. */
export function studyRun(key: string, overrides: Partial<StudyRun> = {}): StudyRun {
  return {
    evaluation_key: key,
    label: 'All-period search',
    params_sentence: 'Gap $0.15 · RSI 48–72 · EMA 8/21 · hold 4 bars',
    window_start_ms: DEVELOPMENT_START_MS,
    window_end_ms: FINAL_START_MS,
    status: 'completed',
    total_trades: 146,
    total_return_pct: 0.087,
    sharpe_ratio: 1.18,
    max_drawdown_pct: 0.064,
    error: null,
    has_curve: true,
    ...overrides,
  };
}

/** A page of runs: the search's winner, a second search point, a run with no curve recorded, a failed run and a fold's test run. */
export function runsPage(overrides: Partial<RunsPage> = {}): RunsPage {
  return {
    total: 5,
    page: 1,
    page_size: 100,
    sort: 'recorded',
    folds: 2,
    groups: [
      { key: 'search', label: 'All-period search', runs: 4, completed: 3, with_curve: 2 },
      { key: 'fold_test', label: 'Fold test', runs: 1, completed: 1, with_curve: 1 },
    ],
    rows: [
      studyRun('run-winner', { label: 'All-period search · All-period fit' }),
      studyRun('run-second', { total_return_pct: 0.041, sharpe_ratio: 0.72, params_sentence: 'Gap $0.10 · RSI 48–72 · EMA 8/21 · hold 4 bars' }),
      studyRun('run-old', { has_curve: false, total_return_pct: 0.02, params_sentence: 'Gap $0.05 · RSI 48–72 · EMA 8/21 · hold 4 bars' }),
      studyRun('run-failed', { status: 'failed', error: 'no bars', has_curve: false, total_trades: 0, total_return_pct: null, sharpe_ratio: null, max_drawdown_pct: null, params_sentence: 'Gap $0.60 · RSI 48–72 · EMA 8/21 · hold 4 bars' }),
      studyRun('run-fold', { label: 'Fold 1 test · Fold winner', window_start_ms: etMidnightMs('2025-07-01'), window_end_ms: etMidnightMs('2025-10-01') }),
    ],
    ...overrides,
  };
}

/** The session closes every fixture curve is read against. */
const CURVE_SESSIONS_MS: readonly number[] = [etMidnightMs('2024-01-02'), etMidnightMs('2024-06-03'), etMidnightMs('2025-12-31')];

/** A run's curve over the three fixture sessions, ending at `end`; it dips a quarter of that from its first peak. */
export function runCurve(key: string, end = 0.087): RunCurve {
  return {
    evaluation_key: key,
    first_session: 0,
    cumulative_return: [0, end / 2, end],
    drawdown: [0, -end / 4, 0],
    deepest_drawdown: -end / 4,
  };
}

export function runCurves(keys: readonly string[], notRecorded: readonly string[] = []): RunCurves {
  return { sessions_ms: [...CURVE_SESSIONS_MS], curves: keys.map((key, i) => runCurve(key, 0.087 - i * 0.02)), not_recorded: [...notRecorded] };
}

/** A step's set of lines: a search's winner, the current settings and one other scored point, unless the set is another. */
export function curveSet(set: CurveSetKey = 'search', overrides: Partial<CurveSet> = {}): CurveSet {
  const settings = 'Gap $0.15 · RSI 48–72 · EMA 8/21 · hold 4 bars';
  const lines: Record<CurveSetKey, CurveSet['lines']> = {
    search: [
      { role: 'incumbent', label: 'Current settings', current_settings: true, params_sentence: 'Gap $0.20 · RSI 50–70 · EMA 5/10 · hold 5 bars', curve: runCurve('run-incumbent', 0.052) },
      { role: 'other', label: 'Scored point', current_settings: false, params_sentence: 'Gap $0.10 · RSI 48–72 · EMA 8/21 · hold 4 bars', curve: runCurve('run-second', 0.041) },
      { role: 'winner', label: 'Winner', current_settings: false, params_sentence: settings, curve: runCurve('run-winner', 0.087) },
    ],
    recent: [{ role: 'winner', label: 'Winner', current_settings: false, params_sentence: 'Gap $0.10 · RSI 45–70 · EMA 5/10 · hold 3 bars', curve: runCurve('run-recent', 0.116) }],
    fold_tests: [
      { role: 'winner', label: 'Fold 1 winner', current_settings: false, params_sentence: settings, curve: runCurve('run-fold', 0.02) },
      { role: 'incumbent', label: 'Fold 1 current settings', current_settings: true, params_sentence: 'Gap $0.20 · RSI 50–70 · EMA 5/10 · hold 5 bars', curve: runCurve('run-fold-inc', 0.01) },
    ],
    candidate: [
      { role: 'candidate', label: 'All-period fit', current_settings: false, params_sentence: settings, curve: runCurve('run-candidate', 0.087) },
      { role: 'stress', label: 'Extra 1¢/share slippage', current_settings: false, params_sentence: settings, curve: runCurve('run-stress', 0.041) },
      { role: 'neighbor', label: 'Fast EMA length one step below (7)', current_settings: false, params_sentence: 'Gap $0.15 · RSI 48–72 · EMA 7/21 · hold 4 bars', curve: runCurve('run-neighbor', 0.052) },
    ],
  };
  return { sessions_ms: [...CURVE_SESSIONS_MS], lines: lines[set], left_out: 0, not_recorded: 0, failed: 0, notes: [], ...overrides };
}
