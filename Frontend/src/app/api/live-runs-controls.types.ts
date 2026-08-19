// Read-only legacy live-run state contract.
//
// Mirrors PythonDataService/app/schemas/live_runs.py. The Angular consumer
// Keep names in lockstep with the retained Python read models.
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

export type CommandVerb =
  | 'PAUSE'
  | 'RESUME'
  | 'STOP'
  | 'FLATTEN'
  | 'MARK_POISONED'
  | 'RECONCILE';
