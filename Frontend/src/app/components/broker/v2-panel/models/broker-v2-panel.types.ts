// Mirror of PythonDataService/app/schemas/broker_v2_panel.py

export type Phase = 'OFF_DUTY' | 'ON_DUTY' | 'RETIRED';
export type DesiredState = 'RUNNING' | 'STOPPED';
export type ActionId =
  | 'deploy'
  | 'start'
  | 'stop'
  | 'flatten_stop'
  | 'retire'
  | 'cancel_order'
  | 'clear_hold'
  | 'reconcile_now';
export type ChannelState = 'healthy' | 'unhealthy' | 'unknown';
export type StationId =
  | 'SIGNAL'
  | 'INTENT'
  | 'SUBMIT_GATE'
  | 'BROKER_ACK'
  | 'FILL'
  | 'RECONCILED';
export type StationState =
  | 'satisfied'
  | 'waiting'
  | 'blocked'
  | 'unknown_stale'
  | 'not_applicable';
export type ReconciliationVerdict =
  | 'clean'
  | 'unexplained_order'
  | 'missing_intent'
  | 'stale';
export type HoldReason =
  | 'NO_HOLD'
  | 'UNEXPLAINED_ORDER_HOLD'
  | 'STREAM_HEALTH_HOLD';
export type DutyOutcomeKind =
  | 'ON_DUTY'
  | 'STOPPED_OUTCOME'
  | 'CRASHED'
  | 'EXITED_UNVERIFIED';

export interface BotCatalogView {
  strategy_instance_id: string;
  broker: string;
  account_id: string;
  symbol: string;
  phase: Phase;
  desired_state: DesiredState;
  running: boolean;
  status_label: string;
  exposure: Record<string, number>;
  fills_today: number;
  realized_pnl_today: number;
  open_pnl: number | null;
  last_activity_at_ms: number | null;
  needs_attention: boolean;
}

export interface OperatorBlocker {
  code: string;
  label: string;
  explanation: string;
  disposition: 'fix_here' | 'fix_elsewhere' | 'wait' | 'terminal';
  action_hint: string | null;
  route: string | null;
}

export interface OperatorConfirmationCopy {
  required: boolean;
  prompt: string;
  ack_phrase: string | null;
}

export interface PanelAction {
  action_id: ActionId;
  label: string;
  explanation: string;
  enabled: boolean;
  blockers: OperatorBlocker[];
  confirmation: OperatorConfirmationCopy | null;
  revision: number;
}

export interface PanelActionRequest {
  action_id: ActionId;
  revision: number;
  idempotency_key: string;
  reason?: string;
}

export interface PanelActionResult {
  action_id: ActionId;
  applied: boolean;
  revision: number;
  message: string;
}

export interface StationApplicability {
  station_id: StationId;
  applicable: boolean;
  label: string;
  explanation: string;
}

export interface PanelProfile {
  broker: string;
  fee_fidelity: 'per_fill' | 'aggregate' | 'none';
  flatten_supported: boolean;
  live_bars_supported: boolean;
  stations: StationApplicability[];
  supported_action_ids: ActionId[];
}
