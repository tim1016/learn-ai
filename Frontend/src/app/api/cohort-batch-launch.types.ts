/** Browser compare token for the server-owned cohort launch command. */
export interface CohortBatchLaunchCommandRequest {
  member_strategy_instance_ids: string[];
}

export interface CohortBatchLaunchMemberOutcome {
  strategy_instance_id: string;
  state: 'accepted' | 'blocked' | 'skipped';
  reason: string;
  next_safe_action: string;
}

export interface CohortBatchLaunchStatus {
  schema_version: number;
  account_id: string;
  cohort_id: string;
  member_strategy_instance_ids: string[];
  window_start_ms: number;
  window_end_ms: number;
  authorized_by: string;
  authorized_recorded_at_ms: number;
  outcomes_state: 'pending' | 'recorded' | 'unreadable';
  outcomes: CohortBatchLaunchMemberOutcome[];
  outcomes_recorded_at_ms: number | null;
  outcomes_error: string | null;
}
