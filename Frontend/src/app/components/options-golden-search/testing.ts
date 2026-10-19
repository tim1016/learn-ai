import { exampleBody } from '../options-backtest/testing/fixtures';
import type { OptionsStudy, Preflight, SearchCandidate, SearchDeclarations } from './options-golden-search.types';

export function declarations(): SearchDeclarations {
  return {
    setup_name: 'Saved two-leg setup', setup: exampleBody(), setup_hash: 'a'.repeat(64),
    knobs: [{ path: 'legs.call.stop_loss.value', label: 'Call stop loss', leg_id: 'call', current_value: '25', value_type: 'decimal', legal_values: [], min: '0.0001', max: '99.9999', step: '0.0001', help: 'The stop rule remains included when held fixed.' }, { path: 'legs.put.reentry_on_stop', label: 'Put re-entry rule', leg_id: 'put', current_value: { kind: 'at_once', count: 1 }, value_type: 'category', legal_values: [null, { kind: 'at_once', count: 1 }], min: null, max: null, step: null, help: 'Include rule Off/On.' }],
    history_bounds: { start_ms: 1704171600000, end_ms: 1790999999999 },
    defaults: { version: 'v1', rationale: 'Editable policy hurdles.', policy: { max_drawdown_percent: '20', min_sharpe: 1, min_activity_per_year: 50, min_daily_observations: 30, min_forward_sessions: 30, min_final_sessions: 30, max_recovery_sessions: null, risk_free_daily_return: 0, stress_gate: false }, train_months: 6, test_months: 2, final_months: 6, evaluation_cap: 5000, finalist_count: 3 },
  };
}
export function preflight(): Preflight {
  return { preflight_hash: 'b'.repeat(64), can_lock: true, missing_history: false, blockers: [], warnings: ['Final activity is not guaranteed.'], drawdown_limit_usd: '2000', combination_count: 2, legal_combination_count: 2, invalid_combination_count: 0, required_evaluations: 30, reserved_evaluations: 20, estimated_setup_sessions: 3000, mark_coverage: 'Verified', exposure: 'History unknown', identity: 'pinned-code-and-data', windows: [] };
}
export function candidate(changes: Partial<SearchCandidate> = {}): SearchCandidate {
  return { candidate_hash: 'c'.repeat(64), label: 'Candidate one', is_original: false, verdict: 'meets_rules', net_profit_usd: '200.01234567890123456789', drawdown_usd: '100', drawdown_fraction: '0.01', drawdown_percent: '1', sharpe: 1.2, completed_sessions: 40, recovery: 'Recovered after 5 sessions', gate_outcomes: [], evidence: [], monthly: [], equity: [], sessions: [], ...changes };
}
export function study(changes: Partial<OptionsStudy> = {}): OptionsStudy {
  return { id: 'study-1', revision: 4, phase: 'locked', status: 'ready', protocol_hash: 'd'.repeat(64), approval_reviews: {}, protocol_summary: [{ label: 'Starting capital', value: '10000 USD' }], setup_name: 'Saved two-leg setup', underlying: 'SPY', progress: { stage: 'locked', completed: 0, queued: 0, running: 0, cache_hits: 0, execution_attempts: 0, spent: 0, reserved: 20, capacity_message: 'Width one' }, candidates: [], final_results: [], gate_outcomes: [], exposure: { classification: 'history_unknown', explanation: 'Outside research cannot be observed.' }, allowed_commands: ['run_research'], dispatch: null, failure_reason: null, qualification_id: null, qualification_status: null, ...changes };
}
