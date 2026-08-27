// Shared operator-notice vocabulary — mirrors
// PythonDataService/app/operator/notices/schema.py. Hand-written; the OpenAPI
// codegen gate covers only graphql/generated and broker.types.ts, so keep the
// names in lockstep with the Python source by hand.
//
// All timestamps are int64 ms UTC on the wire and typed `number` here. Never a
// string. Render through the shared timestamp component, display-side only.

export type OperatorNoticeTier = 'info' | 'warning' | 'critical';

export type OperatorNoticeActionability =
  | 'actuatable'
  | 'routed'
  | 'self_resolving'
  | 'no_remedy';

export type OperatorNoticeRemedyStatus = 'inherent' | 'unbuilt';

export type OperatorNoticeActionKind =
  | 'none'
  | 'open_runbook'
  | 'focus_cockpit_action'
  | 'renew_control_plane_lease'
  | 'external_manual_check'
  | 'redeploy';

export interface OperatorNoticeAction {
  kind: OperatorNoticeActionKind;
  label: string | null;
  target: string | null;
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

// Run-schema mirrors that reuse the notice vocabulary above. Their source is
// PythonDataService/app/schemas/live_runs.py, not notices/schema.py:
// `ExitReason` (:30), `MutationRungReceiptCode` (:94), `MutationRungReceipt`
// (:101), and the `rung_id` union, which the Python side names
// `MutationBlockageStageId` (:82). Same hand-written, ungated status as above.

export type ExitReason =
  | 'normal'
  | 'force_flat_complete'
  | 'keyboard_interrupt'
  | 'signal'
  | 'max_orders_exceeded'
  | 'fatal_halt'
  | 'recovery_flatten'
  | 'exception';

export type MutationRungReceiptCode =
  | 'mutation.next_blocking_rung'
  | 'mutation.scoped_all_clear'
  | 'mutation.observational_warning';

export type MutationRungReceiptStageId =
  | 'control_plane'
  | 'host_process'
  | 'broker'
  | 'account_safety'
  | 'account_clerk'
  | 'reconciliation'
  | 'preflight'
  | 'trading_session'
  | 'runtime_freshness';

export interface MutationRungReceipt {
  code: MutationRungReceiptCode;
  tier: OperatorNoticeTier;
  title: string;
  message: string;
  rung_id: MutationRungReceiptStageId | null;
  source_codes: string[];
  forensic_facts: Record<string, string | number | boolean | null>;
  actionability: OperatorNoticeActionability;
  resolution: string;
  remedy_status: OperatorNoticeRemedyStatus | null;
  action: OperatorNoticeAction;
  occurred_at_ms: number;
}
