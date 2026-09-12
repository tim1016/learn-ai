/**
 * Wire contract for the immutable Validation Golden Run workflow.
 *
 * Python owns the scientific case and the computed parity evidence.  The UI
 * only transports and presents that evidence alongside the human judgment.
 */

export type GoldenReviewDecision = "accept" | "reject";

export interface GoldenReview {
  id: number;
  decision: GoldenReviewDecision;
  classification: "engine_agreement" | "reviewed_deviations" | "manual_override" | null;
  evidence_state: string;
  reason: string;
  quantconnect_backtest_id: string | null;
  authorized_program_version: string | null;
  reviewed_by: string;
  reviewed_at_ms: number;
  expected_evidence_revision: string;
}

export interface GoldenValidationCase {
  schema_version: number;
  source_run_id: number;
  strategy: { name: string; program_version: string | null };
  symbol: string;
  parameters: Record<string, unknown>;
  window: { start_ms: number; end_ms: number; timespan: string };
  data_policy: Record<string, unknown> | null;
  execution: {
    fill_mode: string;
    initial_cash: number;
    commission_per_order: number | null;
    brokerage_policy: string | null;
    configuration: Record<string, unknown> | null;
  };
  requested_engine: string | null;
  parity_group_id: string | null;
}

export interface GoldenValidation {
  id: number;
  source_run_id: number;
  label: string | null;
  strategy_name: string;
  symbol: string;
  rationale: string;
  designated_by: string;
  designated_at_ms: number;
  state: string;
  validation_case: GoldenValidationCase;
  evidence_state: string;
  evidence_revision: string;
  parity_evidence: Record<string, unknown>;
  latest_review: GoldenReview | null;
  review_is_current: boolean | null;
  reviews: GoldenReview[];
}

export interface DesignateGoldenValidationRequest {
  source_run_id: number;
  command_id: string;
  label?: string;
  rationale: string;
}

export interface ReviewGoldenValidationRequest {
  command_id: string;
  expected_evidence_revision: string;
  decision: GoldenReviewDecision;
  reason: string;
  quantconnect_backtest_id?: string;
  authorized_program_version?: string;
}
