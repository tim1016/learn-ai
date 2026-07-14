/** Account-rooted receipt for a deliberate paper-validation cohort launch. */
export interface CohortBatchLaunchCreateRequest {
  cohort_id: string;
  member_strategy_instance_ids: string[];
  window_start_ms: number;
  window_end_ms: number;
  authorized_by: string;
}

export interface CohortBatchLaunchReceipt extends CohortBatchLaunchCreateRequest {
  schema_version: number;
  account_id: string;
  recorded_at_ms: number;
}

export interface CohortBatchLaunchMemberOutcome {
  strategy_instance_id: string;
  state: 'accepted' | 'blocked' | 'skipped';
  reason: string;
  next_safe_action: string;
}

export interface CohortBatchLaunchOutcomesRequest {
  outcomes: CohortBatchLaunchMemberOutcome[];
}

export interface CohortBatchLaunchOutcomesReceipt extends CohortBatchLaunchOutcomesRequest {
  schema_version: number;
  account_id: string;
  cohort_id: string;
  recorded_at_ms: number;
}
