export type OperatorSurfaceConditionSeverity =
  | 'ok'
  | 'info'
  | 'warning'
  | 'critical'
  | 'neutral';

export type BrokerConnectionConditionCode =
  | 'BROKER_CONNECTED'
  | 'BROKER_DISCONNECTED'
  | 'BROKER_DISABLED'
  | 'BROKER_LINK_SOFT_LOST'
  | 'BROKER_SUBSCRIPTIONS_STALE'
  | 'BROKER_DATA_FARM_DEGRADED'
  | 'BROKER_RECONNECTING'
  | 'BROKER_RECOVERING'
  | 'BROKER_HARD_DOWN'
  | 'BROKER_RUNTIME_UNBOUND'
  | 'BROKER_CONNECTION_UNKNOWN';

export interface OperatorSurfaceNamedCondition {
  code: BrokerConnectionConditionCode;
  severity: OperatorSurfaceConditionSeverity;
  title: string;
  summary: string;
  remediation: string | null;
}

export type OperatorSurfaceBlockageStageId =
  | 'control_plane'
  | 'host_process'
  | 'broker'
  | 'account_safety'
  | 'account_clerk'
  | 'reconciliation'
  | 'preflight'
  | 'trading_session'
  | 'runtime_freshness';

export type OperatorSurfaceBlockageState =
  | 'clear'
  | 'info'
  | 'warning'
  | 'danger'
  | 'unknown';

export interface OperatorSurfaceBlockageStage {
  id: OperatorSurfaceBlockageStageId;
  label: string;
  state: OperatorSurfaceBlockageState;
  severity: OperatorSurfaceConditionSeverity;
  current: boolean;
  title: string;
  summary: string;
  next_step: string | null;
  reason_codes: string[];
}

export interface OperatorSurfaceBlockageLadder {
  headline: string;
  summary: string;
  current_stage_id: OperatorSurfaceBlockageStageId | null;
  stages: OperatorSurfaceBlockageStage[];
}
