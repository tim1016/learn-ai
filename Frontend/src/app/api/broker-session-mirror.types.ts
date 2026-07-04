import type { OperatorNotice } from './live-instances.types';

export type BrokerSessionIdentityType =
  | 'bot'
  | 'system'
  | 'orphaned_bot_socket'
  | 'ghost';

export type BrokerSessionRecency =
  | 'current'
  | 'past_closed'
  | 'past_last_known'
  | 'unknown';

export type BrokerSessionObserverStatus = 'online' | 'degraded';
export type BrokerSessionGhostDetectionStatus = 'available' | 'unknown';
export type BrokerSessionRecoveryState =
  | 'HEALTHY'
  | 'LINK_INTERRUPTED'
  | 'RESTORING'
  | 'SOCKET_DOWN'
  | 'RECONNECTING'
  | 'HARD_DOWN';

export type BrokerSessionEventCategory =
  | 'client_lifecycle'
  | 'link_connectivity'
  | 'recovery_reconnect'
  | 'data_farm'
  | 'auth_session'
  | 'order_execution'
  | 'pacing_throttling'
  | 'fault_client_error'
  | 'unclassified';

export type BrokerSessionEventSeverity = 'info' | 'warning' | 'critical';

export type BrokerSessionAttentionCode =
  | 'REGISTRY_SAYS_OFFLINE_BUT_SOCKET_LIVE'
  | 'STARTED_BUT_NO_SOCKET'
  | 'SOCKET_WITHOUT_LIVE_PID'
  | 'ORPHANED_BOT_SOCKET'
  | 'GHOST_SOCKET'
  | 'GHOST_DETECTION_UNAVAILABLE'
  | 'CLIENT_SIGNAL_STALE';

export interface BrokerSessionRegistryClaim {
  state: string;
  run_id: string | null;
  pid: number | null;
  run_dir: string | null;
  started_at_ms: number | null;
  ended_at_ms: number | null;
}

export interface BrokerSessionRosterRow {
  row_id: string;
  identity_type: BrokerSessionIdentityType;
  recency: BrokerSessionRecency;
  socket_present: boolean;
  strategy_instance_id: string | null;
  run_id: string | null;
  account_id: string | null;
  posture: string | null;
  client_id: number | null;
  pid: number | null;
  command: string | null;
  run_dir: string | null;
  local_port: number | null;
  remote_host: string | null;
  remote_port: number | null;
  connection_state: string | null;
  recovery_state: BrokerSessionRecoveryState | null;
  connection_epoch: number | null;
  last_event_ms: number | null;
  as_of_ms: number;
  event_counts: Partial<Record<BrokerSessionEventCategory, number>>;
  attention_codes: BrokerSessionAttentionCode[];
  registry_claim: BrokerSessionRegistryClaim | null;
  notice: OperatorNotice | null;
}

export interface BrokerSessionMirrorSnapshot {
  as_of_ms: number;
  gateway_port: number;
  observer_status: BrokerSessionObserverStatus;
  ghost_detection_status: BrokerSessionGhostDetectionStatus;
  rows: BrokerSessionRosterRow[];
  degradation_reasons: string[];
}

export interface BrokerSessionEvent {
  seq: number;
  ts_ms: number;
  category: BrokerSessionEventCategory;
  severity: BrokerSessionEventSeverity;
  label: string;
  message: string | null;
  raw_event_type: string;
  client_id: number | null;
  account_id: string | null;
  ibkr_code: number | null;
  connection_state: string | null;
  raw: Record<string, unknown>;
}

export interface BrokerSessionEventPage {
  rows: BrokerSessionEvent[];
  next_seq: number | null;
}

export const BROKER_SESSION_PURGE_CONFIRM =
  'PURGE_BROKER_SESSION_DIAGNOSTICS' as const;

export interface BrokerSessionEventPurgeRequest {
  client_id?: number | null;
  start_ms?: number | null;
  end_ms?: number | null;
  confirm: typeof BROKER_SESSION_PURGE_CONFIRM;
}

export interface BrokerSessionEventPurgeResult {
  purged_count: number;
  remaining_count: number;
}
