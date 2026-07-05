export type StrategyValidationState = 'validated' | 'needs_validation';

export interface StrategyValidationDiagnostics {
  verdict: string;
  trades_matched: number;
  trades_validated: number;
  pnl_max_abs_diff: string;
  divergence_counts: Record<string, number>;
  notes: string[];
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
}

export interface StrategyValidationDetail extends StrategyValidationSummary {
  reference_code: StrategyReferenceCode | null;
}

export interface StrategyValidationCatalog {
  strategies: StrategyValidationSummary[];
}
