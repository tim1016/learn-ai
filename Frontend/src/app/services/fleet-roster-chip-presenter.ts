import type { FleetRosterRow, ReadinessVerdictEnum } from '../api/live-instances.types';

export type FleetRosterChipState = 'ok' | 'down' | 'warn' | 'unknown';

export interface FleetRosterChip {
  readonly id: string;
  readonly label: string;
  readonly processState: string;
  readonly readinessVerdict: ReadinessVerdictEnum;
  readonly state: FleetRosterChipState;
}

/**
 * Interim presenter until Stage 8 exposes backend-authored OperatorBlocker
 * chips. Keep this isolated so frontend judgment over roster codes does not
 * settle into the shared connectivity orchestration path.
 */
export function presentFleetRosterChips(
  rows: readonly FleetRosterRow[] | undefined,
): FleetRosterChip[] {
  return (rows ?? [])
    .filter((row) => readinessVerdict(row) !== 'READY')
    .map((row) => ({
      id: row.strategy_instance_id,
      label: row.strategy_instance_id,
      processState: row.process_state,
      readinessVerdict: readinessVerdict(row),
      state: rosterChipState(row),
    }));
}

function rosterChipState(row: FleetRosterRow): FleetRosterChipState {
  if (row.process_state === 'unreachable') return 'down';
  const verdict = readinessVerdict(row);
  if (verdict === 'READY') return 'ok';
  if (verdict === 'BLOCKED' || verdict === 'DEGRADED') return 'warn';
  return 'unknown';
}

function readinessVerdict(row: FleetRosterRow): ReadinessVerdictEnum {
  return row.readiness_verdict ?? 'UNKNOWN';
}
