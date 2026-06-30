export type LifecycleChartStatus =
  | 'passed'
  | 'active'
  | 'blocked'
  | 'poison'
  | 'freeze'
  | 'inactive'
  | 'unknown';

export type LifecycleChartLane = 'bot' | 'account' | 'broker' | 'recovery';

export interface LifecycleChartReceipt {
  label: string;
  value: string;
  unit: string | null;
  source: string | null;
  gate_id: string | null;
  ts_ms: number | null;
  ts_ms_resolved: boolean;
}

export interface LifecycleChartNode {
  id: string;
  label: string;
  technical_label: string | null;
  lane: LifecycleChartLane;
  status: LifecycleChartStatus;
  status_label: string;
  summary?: string | null;
  why?: string | null;
  operator_next_step?: string | null;
  expandable: boolean;
  subgraph_id: string | null;
  evidence_summary: string | null;
  ts_ms: number | null;
  ts_ms_resolved: boolean;
  receipts: LifecycleChartReceipt[];
}

export interface LifecycleChartEdge {
  id: string;
  source: string;
  target: string;
  status: LifecycleChartStatus;
  label: string | null;
  animated: boolean;
}

export type LifecycleChartActionId =
  | 'start_process'
  | 'resume'
  | 'pause'
  | 'flatten_and_pause'
  | 'stop'
  | 'mark_poisoned'
  | 'redeploy';

export interface LifecycleChartAction {
  id: LifecycleChartActionId;
  label: string;
  enabled: boolean;
  reason: string | null;
  target_node_id: string | null;
  tone: 'primary' | 'secondary' | 'danger';
}

export interface LifecycleChartGraph {
  graph_id: string;
  title: string;
  primary_node_id: string;
  nodes: LifecycleChartNode[];
  edges: LifecycleChartEdge[];
}

export interface BotLifecycleChartView {
  chart_id: string;
  selected_bot_id: string;
  title: string;
  global_graph: LifecycleChartGraph;
  subgraphs: Record<string, LifecycleChartGraph>;
  actions: LifecycleChartAction[];
}

export type LifecycleEventSeverity = 'info' | 'warning' | 'critical';
export type LifecycleSafetySeverity = Extract<LifecycleEventSeverity, 'warning' | 'critical'>;
export type LifecycleEventCategory =
  | 'decision'
  | 'risk_gate'
  | 'order'
  | 'fill'
  | 'position_change'
  | 'account_balance'
  | 'freeze'
  | 'halt'
  | 'poison'
  | 'desired_state'
  | 'lifecycle_transition'
  | 'account_event'
  | 'evidence';

export interface LifecycleProjectionEventRow {
  id: number | null;
  account_id: string;
  strategy_instance_id: string | null;
  run_id: string | null;
  event_id: string;
  event_type: string;
  category: LifecycleEventCategory;
  node_id: string | null;
  gate_id: string | null;
  status: LifecycleChartStatus | null;
  severity: LifecycleEventSeverity;
  ts_ms: number | null;
  ts_ms_resolved: boolean;
  source_artifact: string;
  source_type: string;
  source_seq: number | null;
  source_offset: number | null;
  source_hash: string | null;
  summary: string;
  why: string | null;
  operator_next_step: string | null;
  receipt_payload: Record<string, unknown>;
  evidence_refs: Record<string, unknown>[];
  rendered_headline: string | null;
  rendered_template_id: string | null;
  inserted_at_ms: number | null;
  updated_at_ms: number | null;
}

export interface LifecycleTimelineResponse {
  projection_available: boolean;
  canonical_fallback_required: boolean;
  rows: LifecycleProjectionEventRow[];
}

export type LifecycleSafetyTriageResponse = LifecycleTimelineResponse;
