export type LifecycleChartStatus =
  | 'passed'
  | 'active'
  | 'blocked'
  | 'poison'
  | 'freeze'
  | 'inactive'
  | 'unknown';

export type LifecycleChartLane = 'bot' | 'account' | 'broker' | 'recovery';
export type LifecycleChartActionability =
  | 'operator-actionable'
  | 'system-only'
  | 'no-action-needed';

export interface LifecycleChartReceipt {
  label: string;
  value: string;
  headline: string | null;
  detail: string | null;
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
  operator_actionability: LifecycleChartActionability;
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
  source_handle: string | null;
  target_handle: string | null;
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
  reason_code: string | null;
  reason_headline: string;
  reason_detail: string;
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
  only_fresh_run_available: boolean;
}
