export type DaemonDiagnosticStatus = 'pass' | 'warn' | 'fail' | 'skip';
export type DaemonReportStatus = 'pass' | 'warn' | 'fail';
export type DaemonDiagnosticScope = 'global' | 'account' | 'instance' | 'run';
export type DaemonTransport =
  | 'CONNECTED'
  | 'RETRYING'
  | 'UNREACHABLE'
  | 'AUTH_FAILED'
  | 'PROTOCOL_ERROR'
  | 'INCOMPATIBLE_CONTRACT';

export type DaemonDiagnosticCategory =
  | 'reachability'
  | 'auth'
  | 'contract'
  | 'code_freshness'
  | 'lease'
  | 'boot'
  | 'process_registry'
  | 'orphans'
  | 'socket_probe'
  | 'process'
  | 'sockets'
  | 'runtime_freshness'
  | 'artifacts';

export type DaemonDominantCondition =
  | 'healthy'
  | 'instance_healthy'
  | 'unreachable'
  | 'retrying'
  | 'auth_failed'
  | 'malformed_response'
  | 'build_mismatch'
  | 'stale_code'
  | 'lease_stale'
  | 'lease_unwritable'
  | 'boot_changed'
  | 'registry_snapshot_unavailable'
  | 'orphans_present'
  | 'socket_probe_unavailable'
  | 'not_started'
  | 'process_exited'
  | 'registry_amnesia'
  | 'no_socket'
  | 'orphaned_socket'
  | 'runtime_stale'
  | 'run_dir_invisible'
  | 'account_frozen'
  | 'crash_retired_blocked';

export interface DaemonDiagnosticEvidence {
  facts: Record<string, unknown>;
  redacted: boolean;
}

export interface DaemonDiagnosticAction {
  action_id: string;
  kind: 'recovery_mutation' | 'navigation';
  label: string;
  endpoint: string | null;
  confirm: boolean;
  deep_link: string | null;
}

export interface DaemonDiagnosticCheck {
  check_id: string;
  category: DaemonDiagnosticCategory;
  status: DaemonDiagnosticStatus;
  title: string;
  summary: string;
  technical_detail: string | null;
  remediation: string | null;
  scope: DaemonDiagnosticScope;
  scope_ref: string | null;
  evidence: DaemonDiagnosticEvidence | null;
  action: DaemonDiagnosticAction | null;
}

export interface DaemonDiagnosticHeadline {
  title: string;
  summary: string;
  remediation: string | null;
}

export interface DaemonInstanceDiagnostic {
  strategy_instance_id: string;
  overall_status: DaemonReportStatus;
  dominant_condition: DaemonDominantCondition;
  headline: DaemonDiagnosticHeadline;
  checks: DaemonDiagnosticCheck[];
}

export interface DaemonDiagnosticReport {
  overall_status: DaemonReportStatus;
  transport: DaemonTransport;
  dominant_condition: DaemonDominantCondition;
  headline: DaemonDiagnosticHeadline;
  checks: DaemonDiagnosticCheck[];
  per_instance: DaemonInstanceDiagnostic[];
  daemon_boot_id: string | null;
  fetched_at_ms: number;
}
