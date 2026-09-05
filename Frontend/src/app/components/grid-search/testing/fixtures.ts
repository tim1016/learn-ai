import type { StrategyInfo } from '../../strategy-lab/strategy-lab.models';
import type { GridSearchDetail } from '../grid-search.types';

/** A sweepable two-parameter strategy, as the Grid Search and Walk-Forward specs need it. */
export function sweepableStrategy(overrides: Partial<StrategyInfo> = {}): StrategyInfo {
  return {
    name: 'sma_crossover',
    display_name: 'SMA Crossover',
    description: '',
    params_schema: {
      properties: {
        symbol: { type: 'string', default: 'SPY' },
        short_window: { type: 'integer', default: 10, minimum: 2, maximum: 500, title: 'Short window' },
        long_window: { type: 'integer', default: 30, minimum: 3, maximum: 1000, title: 'Long window' },
      },
    },
    supported_resolutions: ['minute'],
    strategy_bars: { timespan: 'minute', multiplier: 15 },
    recency_supported: true,
    sweep_eligibility: { eligible: true, reason_codes: [], offending_parameters: [] },
    ...overrides,
  };
}

export function gridSearchDetail(overrides: Partial<GridSearchDetail> = {}): GridSearchDetail {
  return {
    id: 'abc',
    owner: { kind: 'user', owner_id: null, fold_index: null, phase: null },
    strategy_key: 'sma_crossover',
    symbol: 'SPY',
    status: 'completed',
    job_id: 'job-1',
    created_at_ms: 1704171600000,
    finished_at_ms: 1704172600000,
    window_start_ms: 1704171600000,
    window_end_ms: 1735621200000,
    measure: 'sharpe_ratio',
    min_trades: 5,
    expected_cells: 2,
    completed_cells: 2,
    failed_cells: 0,
    leader_params_hash: 'h2',
    leader_params: { short_window: 5, long_window: 30 },
    incomplete: false,
    uncommitted_changes: false,
    failure_reason: null,
    request: {} as never,
    receipt: {},
    resumable: false,
    resume_refusal: 'the search is complete',
    ...overrides,
  };
}
