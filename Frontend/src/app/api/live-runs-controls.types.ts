export type DesiredStatePathStatus = 'ok' | 'absent' | 'corrupt' | 'unknown_no_ledger_binding';
export type DesiredStateValue = 'RUNNING' | 'PAUSED' | 'STOPPED';
export interface DesiredStateView { state: DesiredStateValue | null; updated_at_ms: number | null; updated_by: string | null; reason: string | null; version: number | null; path_status: DesiredStatePathStatus; }
export type CommandVerb = 'PAUSE' | 'RESUME' | 'STOP' | 'FLATTEN' | 'MARK_POISONED' | 'RECONCILE';
export interface CommandSummary { pending_count: number; acked_count: number; latest_verb: CommandVerb | null; latest_seq: number | null; }
export interface LiveRunStatusControlsExtension { strategy_instance_id: string | null; desired_state: DesiredStateView | null; command_summary: CommandSummary | null; }
export type DesiredStateAction = 'pause' | 'resume' | 'stop';
export interface SetDesiredStateRequest { action: DesiredStateAction; reason?: string; updated_by?: string; }
export interface DesiredStateRecordResponse { state: DesiredStateValue; updated_at_ms: number; updated_by: string; reason: string | null; version: number; }
export interface EnqueueCommandRequest { verb: CommandVerb; }
export interface CommandView { seq: number; verb: CommandVerb; }
export interface CommandAckView { seq: number; verb: CommandVerb; outcome: Record<string, unknown>; }
export interface CommandTimelineResponse { pending: CommandView[]; acks: CommandAckView[]; }
