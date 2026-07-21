import type { components } from './broker.types';

/** Server-owned staggered cohort validation profiles. */
export type CohortStaggerProfileName =
  | 'paper_three_bot_stagger_v2'
  | 'paper_five_bot_stagger_v2'
  | 'paper_five_bot_stagger_v3';

export type CohortBatchLaunchMemberOutcomeReason =
  components['schemas']['CohortBatchLaunchMemberOutcomeRequest']['reason'];

/** Browser compare token for the server-owned cohort launch command. */
export interface CohortBatchLaunchCommandRequest {
  member_strategy_instance_ids: string[];
  launch_profile?: CohortStaggerProfileName;
}

export interface CohortBatchLaunchMemberOutcome {
  strategy_instance_id: string;
  state: 'accepted' | 'blocked' | 'skipped';
  reason: CohortBatchLaunchMemberOutcomeReason;
  next_safe_action: string;
}

export interface CohortEvidenceMember {
  strategy_instance_id: string;
  run_id: string | null;
  verdict: 'healthy' | 'failed' | 'unknown';
  reason: string | null;
  orders_used: number | null;
  orders_cap: number | null;
}

export interface CohortEvidenceSummary {
  sample_count: number;
  cadence_ms: number;
  healthy_overlap_ms: number;
  verdict: 'healthy' | 'failed' | 'unknown';
  reason: string | null;
  source: 'account_event.cohort_evidence_sample';
  members: CohortEvidenceMember[];
}

export interface CohortValidationCertificate {
  schema_version: 1 | 2;
  account_id: string;
  cohort_id: string;
  member_strategy_instance_ids: string[];
  member_run_ids: Record<string, string>;
  window_start_ms: number;
  window_end_ms: number;
  healthy_overlap_ms: number;
  evidence_verdict: 'healthy' | 'failed' | 'unknown';
  evidence_reason: string | null;
  samples: CohortValidationCertificateSample[];
  round_trips: CohortValidationCertificateRoundTrip[];
  incidents: string[];
  final_broker_net_positions: Record<string, number> | null;
  final_broker_residual: Record<string, number> | null;
  final_journal_exposure: Record<string, Record<string, number>>;
  verdict: 'passed' | 'failed' | 'incomplete';
  reasons: string[];
}

export interface CohortValidationCertificateSample {
  expected_at_ms: number;
  observed_at_ms: number | null;
  account_truth: 'healthy' | 'failed' | 'unknown';
  fleet: 'healthy' | 'failed' | 'unknown';
  members: CohortEvidenceMember[];
  broker_net_positions: Record<string, number> | null;
  broker_residual: Record<string, number> | null;
}

export interface CohortValidationCertificateRoundTrip {
  bot_order_namespace: string;
  order_refs: string[];
  order_ids: number[];
  perm_ids: number[];
  exec_ids: string[];
  saw_nonzero_exposure: boolean;
  round_trip_count: number;
  closed: boolean;
}

export interface CohortBatchLaunchStatus {
  schema_version: number;
  launch_profile?: CohortStaggerProfileName | null;
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
  evidence: CohortEvidenceSummary;
  member_scheduled_start_at_ms?: Record<string, number>;
}
