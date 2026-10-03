import { etMidnightMs } from '../../../shared/date/et-midnight';
import type {
  CandidateDetail,
  CandidateKey,
  DecisionSummary,
  EvidenceCandidate,
  EvidenceView,
  ExamOutcome,
  ExamView,
  ExposureState,
  ExposureView,
  Finding,
  GoldenSearchDefaults,
  GoldenSearchPreflight,
  MeasuredConcentration,
  MeasuredTradeCharts,
  Metrics,
  PairMap,
  PairMapCell,
  ProcedureView,
  ProtocolRequest,
  QualificationDeployOffer,
  QualificationView,
  SearchView,
  StrategyCapability,
  StudyDetail,
  StudyProgress,
  StudyState,
  TradeActivity,
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

export function defaults(overrides: Partial<GoldenSearchDefaults> = {}): GoldenSearchDefaults {
  return {
    ...protocol(),
    seed: { ...INCUMBENT_PARAMS },
    final_sessions_cut: 0,
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
      guidance: { title: 'Prefer evidence that survives small changes', text: 'Less return than the recent fit, with lower drawdown and several useful neighbors.' },
      flags: [],
      params_sentence: 'Gap $0.15 · RSI 48–72 · EMA 8/21 · hold 4 bars',
      fixed_sentence: 'Normalized gap fixed at 0 bps',
      exam_eligible: true,
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
      guidance: { title: 'The extra return comes with a warning', text: 'The largest development return also exceeds your drawdown ceiling.' },
      flags: [{ code: 'DRAWDOWN_ABOVE_CEILING', text: 'Above 12% limit' }],
      params_sentence: 'Gap $0.10 · RSI 45–70 · EMA 5/10 · hold 3 bars',
      fixed_sentence: 'Normalized gap fixed at 0 bps',
      exam_eligible: true,
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
      guidance: { title: 'No change can be the best decision', text: 'Keeping the incumbent is a complete research decision.' },
      flags: [],
      params_sentence: 'Gap $0.20 · RSI 50–70 · EMA 5/10 · hold 5 bars',
      fixed_sentence: 'Normalized gap fixed at 0 bps',
      exam_eligible: false,
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
    trades: trades.map((overrides, i) => ({ ...base[i], ...overrides })),
    histogram: {
      bin_width: 200,
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

/** What the server names as weak about an exam: nothing before an outcome, or when the evidence meets the rules. */
function weaknessOf(exam: Omit<ExamView, 'weakness'>): Finding[] {
  if (exam.outcome === null) return [];
  const found: Finding[] = [];
  if (exam.outcome === 'does_not_meet_rules') {
    const failed = exam.checks.filter((check) => check.status === 'fail').map((check) => check.label.toLowerCase());
    found.push({ code: 'EXAM_DOES_NOT_MEET_RULES', text: failed.length > 0 ? `failing the stated rules (${failed.join(', ')})` : 'failing the stated rules' });
  } else if (exam.outcome !== 'meets_rules') {
    found.push({ code: `EXAM_${exam.outcome.toUpperCase()}`, text: EXAM_WEAKNESS[exam.outcome] });
  }
  if (exam.claim !== 'confirmatory') found.push({ code: `EXPOSURE_${exam.exposure_state.toUpperCase()}`, text: EXPOSURE_WEAKNESS[exam.exposure_state] });
  return found;
}

/** A final test; its `weakness` follows its outcome, claim and exposure as the server's does, unless an override names it. */
export function examView(overrides: Partial<ExamView> = {}): ExamView {
  const exam: Omit<ExamView, 'weakness'> = {
    candidate_key: 'all_period',
    candidate_point: { ...ALL_PERIOD_POINT },
    window: { start_ms: FINAL_START_MS, end_ms: FINAL_END_MS },
    claim: 'confirmatory',
    exposure_state: 'not_opened',
    outcome: 'meets_rules',
    checks: [
      { code: 'NET_POSITIVE', label: 'Profit after stated costs', status: 'pass', detail: 'Net profit $1,840.' },
      { code: 'DRAWDOWN_WITHIN', label: 'Worst drawdown within the ceiling', status: 'pass', detail: '3.1% against 20%.' },
      { code: 'SAMPLE_FLOOR', label: 'Enough trades', status: 'pass', detail: '36 trades against 30.' },
      { code: 'BEATS_INCUMBENT', label: 'At least the incumbent', status: 'pass', detail: 'Sharpe 0.91 against 0.54.' },
    ],
    retention: 0.61,
    candidate_metrics: metrics({ total_return_pct: 0.018, max_drawdown_pct: 0.031, total_trades: 36, sharpe_ratio: 0.91 }),
    incumbent_metrics: metrics({ total_return_pct: 0.009, max_drawdown_pct: 0.042, total_trades: 39, sharpe_ratio: 0.54 }),
    ...overrides,
  };
  return { ...exam, weakness: overrides.weakness ?? weaknessOf(exam) };
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
  awaiting_candidate: { headline: 'Compare the candidates', detail: 'Pick one candidate for the final test, or keep the current settings.' },
  candidate_locked: { headline: 'Open the final test', detail: 'One candidate, one look.' },
  exam_running: { headline: 'The final test is running', detail: 'The candidate and the incumbent run on the held-back dates.' },
  awaiting_review: { headline: 'Decide', detail: 'Approve the candidate or keep the current settings.' },
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
      exam: examJudged ? examView() : examOpened ? examView({ outcome: null, checks: [], retention: null, candidate_metrics: null, incumbent_metrics: null }) : null,
      qualification: qualificationFor(state),
    },
    decision: state === 'approved' ? { kind: 'approve', note: 'Accept this exact configuration after reviewing the stated limitations.', at_ms: etMidnightMs('2026-09-30') } : null,
    candidate_key: examOpened || state === 'candidate_locked' ? 'all_period' : null,
    exam_locked: examOpened,
    exposure_preview: state === 'awaiting_candidate' || state === 'candidate_locked' ? exposure() : null,
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
    case 'candidate_locked':
      return state === 'candidate_locked' ? ['select_candidate', 'open_exam', 'retain', 'revise', 'close'] : ['select_candidate', 'retain', 'revise', 'close'];
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
