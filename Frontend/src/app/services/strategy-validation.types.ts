export type StrategyValidationState = 'validated' | 'needs_validation';
export type StrategyValidationFlag = 'validated' | 'invalidated';
export type BehavioralEquivalenceVerdict = 'accepted_for_deploy' | 'evidence_only' | 'rejected';
export type StrategyCategory = 'production_candidate' | 'operational_validation_harness';
export type StrategyProofState = 'current' | 'stale' | 'missing' | 'blocked' | 'rejected' | 'unreadable';
export type StrategyProofStageState = 'complete' | 'stale' | 'missing' | 'blocked' | 'not_applicable';
export type StrategyArtifactState = 'current' | 'stale' | 'missing' | 'unreadable';

export interface StrategyArtifactCheck {
  label: string;
  ref: string | null;
  state: StrategyArtifactState;
  recorded_sha256: string | null;
  current_sha256: string | null;
}

export interface StrategyProofAction {
  kind: 'external_link';
  label: string;
  href: string;
}

export interface StrategyProofStage {
  stage_id: string;
  title: string;
  state: StrategyProofStageState;
  authority: string;
  summary: string;
  next_step: string | null;
  actions: StrategyProofAction[];
  evidence: StrategyArtifactCheck[];
}

export interface StrategyProofDossier {
  state: StrategyProofState;
  completed_stages: number;
  total_stages: number;
  blocking_stage_id: string | null;
  blocking_summary: string | null;
  stages: StrategyProofStage[];
}

export interface StrategyValidationDiagnostics {
  verdict: string;
  trades_matched: number;
  trades_validated: number;
  pnl_max_abs_diff: string;
  divergence_counts: Record<string, number>;
  notes: string[];
}

export interface StrategyEvidenceSnapshot {
  validator_code_ref?: string | null;
  validator_code_sha256?: string | null;
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
  recorded_sha256: string | null;
  state: StrategyArtifactState;
  language: string;
  source: string;
}

export interface StrategyValidationSummary {
  strategy_key: string;
  display_name: string;
  description: string;
  strategy_category: StrategyCategory;
  validation_state: StrategyValidationState;
  deployable: boolean;
  proof: StrategyProofDossier;
  validator_code_ref?: string | null;
  validator_code_sha256?: string | null;
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
  qc_cloud_backtest_id?: string;
}

export interface StrategyValidationRefreshResult {
  refresh_id: string;
  refreshed_at_ms: number;
  detail: StrategyValidationDetail;
}
