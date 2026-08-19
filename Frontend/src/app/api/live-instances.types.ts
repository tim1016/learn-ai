/** Retained account-evidence, receipt, and shared operator-notice contracts. */

import type {
  OperatorNoticeAction,
  OperatorNoticeActionability,
  OperatorNoticeRemedyStatus,
  OperatorNoticeTier,
} from './live-runs.types';

export type {
  MutationRungReceipt,
  MutationRungReceiptCode,
  MutationRungReceiptStageId,
  OperatorNoticeAction,
  OperatorNoticeActionability,
  OperatorNoticeActionKind,
  OperatorNoticeRemedyStatus,
  OperatorNoticeTier,
} from './live-runs.types';

export type GateResultStatus =
  | 'pass'
  | 'block'
  | 'poison'
  | 'freeze'
  | 'unknown'
  | 'not_applicable';

export interface GateResult {
  gate_id: string;
  status: GateResultStatus;
  source: string;
  operator_reason: string;
  operator_next_step: string | null;
  evidence_at_ms: number;
}

export type OperatorNoticeCode =
  | 'runtime.market_closed'
  | 'runtime.market_session_halted'
  | 'runtime.market_data_stale'
  | 'runtime.market_data_first_bar_timeout'
  | 'runtime.market_data_feed_stalled'
  | 'runtime.broker_probe_stale'
  | 'runtime.broker_probe_missing'
  | 'runtime.command_loop_unresponsive'
  | 'runtime.engine_runtime_incompatible'
  | 'runtime.control_plane_lease_stale'
  | 'runtime.control_plane_boot_id_mismatch'
  | 'watchdog.flatten_completed'
  | 'watchdog.flatten_not_needed'
  | 'watchdog.flatten_timed_out'
  | 'watchdog.flatten_failed'
  | 'watchdog.broker_disconnected_before_flatten'
  | 'activity.publisher_starting'
  | 'activity.publisher_not_running'
  | 'activity.publisher_degraded'
  | 'activity.source_blind_to_bot_orders'
  | 'activity.dropped_paused_intent'
  | 'reconciliation.required_after_uncertain_flatten'
  | 'reconciliation.discovered_execution_not_in_engine_state'
  | 'reconciliation.divergence_while_submitting'
  | 'fleet.sibling_liveness_unproven'
  | 'broker_session.orphaned_socket'
  | 'order.rejected'
  | 'submit.uncertain'
  | 'submit.halted'
  | 'submit.launch_failed'
  | 'submit.unmapped_diagnostic'
  | 'safety_halt.poisoned';

export interface OperatorNotice {
  code: OperatorNoticeCode;
  tier: OperatorNoticeTier;
  title: string;
  message: string;
  source_codes: string[];
  forensic_facts: Record<string, string | number | boolean | null>;
  actionability: OperatorNoticeActionability;
  resolution: string;
  remedy_status: OperatorNoticeRemedyStatus | null;
  action: OperatorNoticeAction;
  runbook_slug: string | null;
  occurred_at_ms: number | null;
}

export interface OperatorIncident {
  schema_version: number;
  incident_id: string;
  category: 'watchdog' | 'activity' | 'reconciliation' | 'order' | 'submit' | 'safety-halt';
  notice: OperatorNotice;
  started_at_ms: number;
  resolved_at_ms: number | null;
  evidence: Record<string, unknown>;
}
