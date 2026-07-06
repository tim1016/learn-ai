export type StrategyValidationState = 'validated' | 'needs_validation';
export type StrategyValidationFlag = 'validated' | 'invalidated';
export type BehavioralEquivalenceVerdict = 'accepted_for_deploy' | 'evidence_only' | 'rejected';

export interface StrategyValidationDiagnostics {
  verdict: string;
  trades_matched: number;
  trades_validated: number;
  pnl_max_abs_diff: string;
  divergence_counts: Record<string, number>;
  notes: string[];
}

export interface StrategyEvidenceSnapshot {
  settings_file_ref: string | null;
  settings_file_sha256: string | null;
  qc_cloud_backtest_id: string | null;
  audit_copy_ref: string | null;
  audit_copy_sha256: string | null;
  reconciliation_ref: string | null;
  validation_case_symbol: string | null;
  reconciliation_status: string | null;
  diagnostics: StrategyValidationDiagnostics | null;
}

export interface StrategyBehavioralEquivalence {
  verdict: BehavioralEquivalenceVerdict;
  detail: string;
  tolerance?: string | null;
  tolerance_reason?: string | null;
  gating_divergence_counts?: Record<string, number>;
}

export interface StrategyValidationFlagEvent {
  event_id: string;
  event_version?: '1.0';
  strategy_key: string;
  flag: StrategyValidationFlag;
  flagged_by: string;
  flagged_at_ms: number;
  reason: string;
  behavioral_equivalence: StrategyBehavioralEquivalence;
  evidence_snapshot: StrategyEvidenceSnapshot;
  evidence_snapshot_sha256: string;
  superseded_by_event_id: string | null;
}

export interface StrategyReferenceCode {
  path: string;
  sha256: string;
  language: string;
  source: string;
}

export interface StrategyValidationSummary {
  strategy_key: string;
  display_name: string;
  description: string;
  validation_state: StrategyValidationState;
  deployable: boolean;
  settings_file_ref: string | null;
  settings_file_sha256: string | null;
  qc_cloud_backtest_id: string | null;
  audit_copy_ref: string | null;
  audit_copy_sha256: string | null;
  reconciliation_ref: string | null;
  validation_case_symbol: string | null;
  reconciliation_status: string | null;
  diagnostics: StrategyValidationDiagnostics | null;
  behavioral_equivalence: StrategyBehavioralEquivalence | null;
  current_flag_event: StrategyValidationFlagEvent | null;
  flag_events: StrategyValidationFlagEvent[];
}

export interface StrategyValidationDetail extends StrategyValidationSummary {
  reference_code: StrategyReferenceCode | null;
}

export interface StrategyValidationCatalog {
  strategies: StrategyValidationSummary[];
}

export interface StrategyValidationFlagRequest {
  flag: StrategyValidationFlag;
  reason: string;
}

export interface StrategyValidationRefreshResult {
  refresh_id: string;
  refreshed_at_ms: number;
  detail: StrategyValidationDetail;
}
