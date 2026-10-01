import { etMidnightMs } from '../../../shared/date/et-midnight';
import type {
  GoldenSearchDefaults,
  GoldenSearchPreflight,
  Metrics,
  ProcedureView,
  ProtocolRequest,
  StrategyCapability,
  StudyDetail,
  StudyState,
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
      { name: 'gap', label: 'Crossover gap', unit: 'price ($)', kind: 'decimal', domain_low: 0, domain_high: 2, quantum: 0.01, default_low: 0, default_high: 0.6, neighbor_step: 0.05, searchable_by_default: true, warmup_dependent: false, default_value: 0.2 },
      { name: 'rsi_min', label: 'RSI lower gate', unit: 'RSI points', kind: 'decimal', domain_low: 0, domain_high: 100, quantum: 1, default_low: 30, default_high: 60, neighbor_step: 2, searchable_by_default: true, warmup_dependent: false, default_value: 50 },
      { name: 'rsi_max', label: 'RSI upper gate', unit: 'RSI points', kind: 'decimal', domain_low: 0, domain_high: 100, quantum: 1, default_low: 60, default_high: 90, neighbor_step: 2, searchable_by_default: true, warmup_dependent: false, default_value: 70 },
      { name: 'fast_period', label: 'Fast EMA length', unit: 'decision bars', kind: 'integer', domain_low: 2, domain_high: 30, quantum: 1, default_low: 3, default_high: 12, neighbor_step: 1, searchable_by_default: true, warmup_dependent: true, default_value: 5 },
      { name: 'slow_period', label: 'Slow EMA length', unit: 'decision bars', kind: 'integer', domain_low: 3, domain_high: 40, quantum: 1, default_low: 8, default_high: 30, neighbor_step: 1, searchable_by_default: true, warmup_dependent: true, default_value: 10 },
      { name: 'hold_bars', label: 'Hold time', unit: 'decision bars', kind: 'integer', domain_low: 1, domain_high: 26, quantum: 1, default_low: 2, default_high: 12, neighbor_step: 1, searchable_by_default: true, warmup_dependent: false, default_value: 5 },
      { name: 'gap_bps', label: 'Crossover gap (bps)', unit: 'basis points', kind: 'decimal', domain_low: 0, domain_high: 100, quantum: 0.5, default_low: 0, default_high: 5, neighbor_step: 0.5, searchable_by_default: false, warmup_dependent: false, default_value: 0 },
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
  };
}

export const INCUMBENT_PARAMS = { gap: 0.2, gap_bps: 0, rsi_min: 50, rsi_max: 70, symbol: 'SPY' } as const;

export function protocol(overrides: Partial<ProtocolRequest> = {}): ProtocolRequest {
  return {
    strategy_key: 'ema_crossover_signal',
    symbol: 'SPY',
    method: 'zoom',
    knobs: [
      { name: 'gap', mode: 'search', low: 0, high: 0.6, fixed_value: 0.2, grid_step: null },
      { name: 'rsi_min', mode: 'search', low: 30, high: 60, fixed_value: 50, grid_step: null },
      { name: 'rsi_max', mode: 'search', low: 60, high: 90, fixed_value: 70, grid_step: null },
      { name: 'fast_period', mode: 'search', low: 3, high: 12, fixed_value: 5, grid_step: null },
      { name: 'slow_period', mode: 'search', low: 8, high: 30, fixed_value: 10, grid_step: null },
      { name: 'hold_bars', mode: 'search', low: 2, high: 12, fixed_value: 5, grid_step: null },
      { name: 'gap_bps', mode: 'fixed', low: 0, high: 5, fixed_value: 0, grid_step: null },
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

export function defaults(overrides: Partial<GoldenSearchDefaults> = {}): GoldenSearchDefaults {
  return {
    ...protocol(),
    incumbent_label: 'Registry validated settings',
    exposure: { state: 'not_opened', ledger_overlaps: 0, outside_activity_overlaps: 0, explanation: 'No recorded research has opened these dates.' },
    ...overrides,
  };
}

export function preflight(overrides: Partial<GoldenSearchPreflight> = {}): GoldenSearchPreflight {
  return {
    refusals: [],
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
    exposure: { state: 'not_opened', ledger_overlaps: 0, outside_activity_overlaps: 0, explanation: 'No recorded research has opened these dates.' },
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
      { pass_index: 0, knob: 'fast_period', round_index: 0, low: 3, high: 12, values: [3, 5, 8, 10, 12], invalid: [[12, 'The fast EMA must be shorter than the slow EMA.']], results: [[3, 'TOO_FEW_TRADES'], [5, null], [8, null], [10, null]], chosen: 8, moved: true, current_before: 5 },
      { pass_index: 0, knob: 'slow_period', round_index: 0, low: 8, high: 30, values: [8, 13, 19, 24, 30], invalid: [], results: [[10, null], [13, null], [19, null], [24, null], [30, null]], chosen: 10, moved: false, current_before: 10 },
    ],
    edge_hits: ['rsi_max'],
    evaluations: 63,
    incomplete: false,
    ...overrides,
  };
}

export function validationView(overrides: Partial<ValidationView> = {}): ValidationView {
  const [first, second] = preflight().folds;
  return {
    folds: [
      { ...first, status: 'completed', winner: { ...INCUMBENT_PARAMS, fast_period: 8 }, winner_hash: 'f0', train_metrics: metrics({ sharpe_ratio: 1.4 }), test_metrics: metrics({ sharpe_ratio: 0.9, total_return_pct: 0.021 }), failure_reason: null },
      { ...second, status: 'failed', winner: null, winner_hash: null, train_metrics: null, test_metrics: null, failure_reason: 'NO_ELIGIBLE_CANDIDATE' },
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
      { fold_index: 0, test_end_ms: first.test_end_ms, linked_return: 0.021 },
      { fold_index: 1, test_end_ms: second.test_end_ms, linked_return: null },
    ],
    explanation: 'Each fold searched only its own training months.',
    ...overrides,
  };
}

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
  qualification_failed: { headline: 'Qualification failed', detail: 'The current default is unchanged.' },
  retained: { headline: 'Current settings retained', detail: 'The study stays in history.' },
  closed: { headline: 'Study closed', detail: 'The study stays in history.' },
};

export function studyDetail(state: StudyState, overrides: Partial<StudyDetail> = {}): StudyDetail {
  const reachedSearch = !['locked', 'search_running'].includes(state);
  const reachedValidation = reachedSearch && !['awaiting_validation', 'validation_running'].includes(state);
  return {
    id: 'study-0001-aaaa',
    parent_study_id: null,
    strategy_key: 'ema_crossover_signal',
    symbol: 'SPY',
    state,
    presented_status: state.endsWith('_running') ? 'running' : 'idle',
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
    failure_reason: null,
    hidden: false,
    exposure_claim: null,
    exam_outcome: null,
    qualification_id: null,
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
    permitted_actions: state === 'locked' || state === 'awaiting_validation' ? ['continue', 'revise', 'close'] : state.endsWith('_running') ? ['cancel'] : ['revise'],
    action_refusals: {},
    guidance: GUIDANCE[state],
    progress: state.endsWith('_running') ? { stage: state.replace('_running', ''), completed: 120, total_max: 410 } : null,
    dispatch: null,
    results: {
      search: reachedSearch ? procedureView() : null,
      recent: reachedSearch ? procedureView({ winner_hash: 'r1', stop_reason: 'pass_limit', stop_explanation: 'The pass limit was reached while knobs still moved.', edge_hits: [] }) : null,
      validation: reachedValidation ? validationView() : null,
      evidence: null,
      exam: null,
      qualification: null,
    },
    decision: null,
    candidate_key: null,
    exam_locked: false,
    ...overrides,
  };
}
