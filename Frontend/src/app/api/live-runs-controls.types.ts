// Live-runs control contract (PRD-A: UI-1 status additions, UI-3, UI-4).
//
// Mirrors PythonDataService/app/schemas/live_runs.py. The Angular consumer
// (UI-2/UI-3/UI-4) is a sibling PR; these types are the shared contract —
// keep names in lockstep with the Python schemas.
//
// All timestamps are int64 ms UTC on the wire and typed `number` here.
// Never a string. Render to America/New_York display-side only.

export type DesiredStatePathStatus =
  | 'ok'
  | 'absent'
  | 'corrupt'
  | 'unknown_no_ledger_binding';

export type DesiredStateValue = 'RUNNING' | 'PAUSED' | 'STOPPED';

export interface DesiredStateView {
  state: DesiredStateValue | null;
  updated_at_ms: number | null;
  updated_by: string | null;
  reason: string | null;
  version: number | null;
  path_status: DesiredStatePathStatus;
}

export interface CommandSummary {
  pending_count: number;
  acked_count: number;
  latest_verb: CommandVerb | null;
  latest_seq: number | null;
}

// UI-1: these three fields are added to the existing LiveRunStatus response.
export interface LiveRunStatusControlsExtension {
  strategy_instance_id: string | null;
  desired_state: DesiredStateView | null;
  command_summary: CommandSummary | null;
}

// --- UI-3: durable desired-state write API ---

export type DesiredStateAction = 'pause' | 'resume' | 'stop';

export interface SetDesiredStateRequest {
  action: DesiredStateAction;
  reason?: string;
  updated_by?: string;
}

export interface DesiredStateRecordResponse {
  state: DesiredStateValue;
  updated_at_ms: number;
  updated_by: string;
  reason: string | null;
  version: number;
}

// --- UI-4: per-run command-channel API ---

export type CommandVerb =
  | 'PAUSE'
  | 'RESUME'
  | 'STOP'
  | 'FLATTEN'
  | 'MARK_POISONED'
  | 'RECONCILE';

export interface EnqueueCommandRequest {
  verb: CommandVerb;
}

export interface CommandView {
  seq: number;
  verb: CommandVerb;
}

export interface CommandAckView {
  seq: number;
  verb: CommandVerb;
  outcome: Record<string, unknown>;
}

export interface CommandTimelineResponse {
  pending: CommandView[];
  acks: CommandAckView[];
}
