import type {
  FleetRosterRow,
  FleetRosterSnapshot,
  ReadinessVerdictEnum,
} from '../api/live-instances.types';
import {
  openVersionedSnapshotStream,
  type SnapshotStream,
  type SnapshotStreamCallbacks,
} from './versioned-snapshot-stream';

export type FleetRosterStream = SnapshotStream;
export type FleetRosterStreamCallbacks = SnapshotStreamCallbacks<FleetRosterSnapshot>;

const READINESS_VERDICTS = new Set<ReadinessVerdictEnum>([
  'READY',
  'BLOCKED',
  'DEGRADED',
  'UNKNOWN',
]);

export function isFleetRosterSnapshot(value: unknown): value is FleetRosterSnapshot {
  if (typeof value !== 'object' || value === null) return false;
  const record = value as Record<string, unknown>;
  return (
    typeof record['stream_epoch'] === 'string' &&
    isNonNegativeSafeInteger(record['surface_version']) &&
    isNonNegativeSafeInteger(record['fetched_at_ms']) &&
    (
      record['daemon_fetched_at_ms'] === undefined ||
      record['daemon_fetched_at_ms'] === null ||
      isNonNegativeSafeInteger(record['daemon_fetched_at_ms'])
    ) &&
    Array.isArray(record['instances']) &&
    record['instances'].every(isFleetRosterRow)
  );
}

export function openFleetRosterStream(
  callbacks: FleetRosterStreamCallbacks,
): FleetRosterStream {
  return openVersionedSnapshotStream(
    '/api/live-instances/fleet/stream',
    isFleetRosterSnapshot,
    'Fleet roster stream',
    callbacks,
  );
}

function isFleetRosterRow(value: unknown): value is FleetRosterRow {
  if (typeof value !== 'object' || value === null) return false;
  const record = value as Record<string, unknown>;
  const verdict = record['readiness_verdict'];
  return (
    typeof record['strategy_instance_id'] === 'string' &&
    typeof record['process_state'] === 'string' &&
    (verdict === undefined ||
      (typeof verdict === 'string' && READINESS_VERDICTS.has(verdict as ReadinessVerdictEnum))) &&
    (record['readiness_as_of_ms'] === undefined ||
      record['readiness_as_of_ms'] === null ||
      isNonNegativeSafeInteger(record['readiness_as_of_ms']))
  );
}

function isNonNegativeSafeInteger(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
}
