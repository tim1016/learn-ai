import type { ServedSetup } from '../options-backtest/options-backtest.types';

export type SearchValue = string | number | boolean | null | { readonly [key: string]: SearchValue };
export interface SearchKnob {
  readonly path: string;
  readonly label: string;
  readonly leg_id: string | null;
  readonly current_value: SearchValue;
  readonly value_type: 'integer' | 'decimal' | 'boolean' | 'category';
  readonly legal_values: readonly SearchValue[];
  readonly min: string | null;
  readonly max: string | null;
  readonly step: string | null;
  readonly help: string;
}
export interface SearchPolicy {
  readonly rank_by?: 'net_profit' | 'sharpe' | 'drawdown';
  readonly max_drawdown_percent: string;
  readonly min_sharpe: number;
  readonly risk_free_daily_return: number;
  readonly stress_gate: boolean;
  readonly min_activity_per_year: number;
  readonly min_daily_observations: number;
  readonly min_forward_sessions: number;
  readonly min_final_sessions: number;
  readonly max_recovery_sessions: number | null;
}
export interface PlanDefaults {
  readonly version: string;
  readonly rationale: string;
  readonly policy: SearchPolicy;
  readonly train_months: number;
  readonly test_months: number;
  readonly final_months: number;
  readonly evaluation_cap: number;
  readonly finalist_count: number;
}
export interface SearchDeclarations {
  readonly setup_name: string;
  readonly setup: ServedSetup;
  readonly setup_hash: string;
  readonly knobs: readonly SearchKnob[];
  readonly history_bounds: { readonly start_ms: number | null; readonly end_ms: number | null };
  readonly defaults: PlanDefaults;
}
export interface PlanRequest {
  readonly expected_setup_hash: string;
  readonly parent_study_id?: string;
  readonly setup_name: string;
  readonly knobs: readonly { readonly path: string; readonly values: readonly SearchValue[]; readonly priority: number; readonly mode: 'search' }[];
  readonly method: 'zoom' | 'grid';
  readonly policy: SearchPolicy;
  readonly history: { readonly start_ms: number; readonly end_ms: number; readonly train_months: number; readonly test_months: number; readonly final_months: number };
  readonly budget: { readonly evaluation_cap: number };
  readonly zoom: { readonly points: number; readonly refinements: number; readonly passes: number };
  readonly pair_audits: readonly (readonly string[])[];
  readonly finalist_count: number;
  readonly stress: readonly { readonly key: string; readonly slippage_multiplier: string; readonly fee_multiplier: string; readonly slippage_percent?: string; readonly fee_per_contract_usd?: string }[];
}
export interface Preflight {
  readonly preflight_hash: string;
  readonly can_lock: boolean;
  readonly blockers: readonly string[];
  readonly warnings: readonly string[];
  readonly drawdown_limit_usd: string;
  readonly combination_count: number | null;
  readonly legal_combination_count: number | null;
  readonly invalid_combination_count: number | null;
  readonly required_evaluations: number;
  readonly reserved_evaluations: number;
  readonly estimated_setup_sessions: number;
  readonly mark_coverage: string;
  readonly exposure: string;
  readonly identity: string;
  readonly windows: readonly { readonly label: string; readonly start_ms: number; readonly end_ms: number; readonly scheduled_sessions: number; readonly required_traded_sessions: number }[];
}
export interface GateOutcome {
  readonly gate_id: string;
  readonly status: string;
  readonly actual: string | null;
  readonly required: string | null;
  readonly reason: string;
  readonly overridable?: boolean;
}
export interface LegAttempt {
  readonly leg_id: string;
  readonly attempt: number;
  readonly contract: string;
  readonly side: string;
  readonly contracts: number;
  readonly entry_ms: number;
  readonly exit_ms: number;
  readonly entry_price_usd: string;
  readonly exit_price_usd: string;
  readonly entry_fee_usd: string;
  readonly exit_fee_usd: string;
  readonly net_profit_usd: string;
  readonly exit_reason: string;
}
export interface SessionResult {
  readonly session_open_ms: number;
  readonly net_profit_usd: string;
  readonly status: string;
  readonly detail: string;
  readonly attempts: readonly LegAttempt[];
}
export type EvidenceWindow = 'development' | 'final';
export type SessionLoader = (studyId: string, candidateHash: string, window: EvidenceWindow, sessionOpenMs: number) => Promise<SessionResult>;
export interface SearchCandidate {
  readonly candidate_hash: string;
  readonly label: string;
  readonly is_original: boolean;
  readonly verdict: 'meets_rules' | 'does_not_meet_rules' | 'not_enough_evidence' | 'could_not_evaluate';
  readonly net_profit_usd: string | null;
  readonly drawdown_usd: string | null;
  readonly drawdown_fraction: string | null;
  readonly drawdown_percent: string | null;
  readonly sharpe: number | null;
  readonly completed_sessions: number | null;
  readonly recovery: string;
  readonly gate_outcomes: readonly GateOutcome[];
  readonly evidence: readonly { readonly label: string; readonly value: string }[];
  readonly monthly: readonly { readonly month_ms: number; readonly net_profit_usd: string; readonly completed_sessions: number }[];
  readonly equity: readonly { readonly timestamp_ms: number; readonly equity_usd: string; readonly drawdown_usd: string }[];
  readonly sessions: readonly SessionResult[];
}
export type SearchCommand = 'run_research' | 'cancel' | 'resume' | 'open_final' | 'approve' | 'approve_exception' | 'keep_original';
export interface StudyCommand {
  readonly command: SearchCommand;
  readonly expected_revision: number;
  readonly idempotency_key: string;
  readonly candidate_hashes?: readonly string[];
  readonly candidate_hash?: string;
  readonly comment?: string;
  readonly waived_gate_ids?: readonly string[];
}
export interface ApprovalReview {
  readonly failures: readonly GateOutcome[];
  readonly blocked_reason: string | null;
}
export interface OptionsStudy {
  readonly id: string;
  readonly revision: number;
  readonly phase: string;
  readonly status: string;
  readonly protocol_hash: string;
  readonly approval_reviews: Readonly<Record<string, ApprovalReview>>;
  readonly protocol_summary: readonly { readonly label: string; readonly value: string }[];
  readonly setup_name: string;
  readonly underlying: string;
  readonly progress: { readonly stage: string; readonly completed: number; readonly queued: number; readonly running: number; readonly cache_hits: number; readonly execution_attempts: number; readonly spent: number; readonly reserved: number; readonly capacity_message: string };
  readonly candidates: readonly SearchCandidate[];
  readonly final_results: readonly SearchCandidate[];
  readonly gate_outcomes: readonly GateOutcome[];
  readonly exposure: { readonly classification: string; readonly explanation: string };
  readonly allowed_commands: readonly SearchCommand[];
  readonly dispatch: { readonly study_id: string; readonly stage_token: string } | null;
  readonly failure_reason: string | null;
  readonly qualification_id: string | null;
  readonly qualification_status: string | null;
}

export type StudySummary = Pick<OptionsStudy, 'id' | 'setup_name' | 'underlying' | 'phase' | 'status'> & { readonly updated_at_ms: number };
