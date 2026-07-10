import type {
  FleetRosterSnapshot,
  LiveInstanceSummary,
  ReadinessVerdictEnum,
} from '../api/live-instances.types';
import type { AuthenticatedSseStatus } from './authenticated-sse-connection';
import { openAuthenticatedSseConnection } from './authenticated-sse-connection';

export interface FleetRosterStream {
  readonly close: () => void;
}

export interface FleetRosterStreamCallbacks {
  readonly onSnapshot: (snapshot: FleetRosterSnapshot) => void;
  readonly onMalformedSnapshot: (message: string) => void;
  readonly onStatus: (status: AuthenticatedSseStatus) => void;
}

const READINESS_VERDICTS = new Set<ReadinessVerdictEnum>([
  'READY',
  'BLOCKED',
  'DEGRADED',
  'UNKNOWN',
]);

export function adoptFleetRosterSnapshot(
  current: FleetRosterSnapshot | null,
  candidate: FleetRosterSnapshot,
): FleetRosterSnapshot {
  if (current === null || current.stream_epoch !== candidate.stream_epoch) return candidate;
  return candidate.surface_version > current.surface_version ? candidate : current;
}

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
    record['instances'].every(isLiveInstanceSummary)
  );
}

export function openFleetRosterStream(
  callbacks: FleetRosterStreamCallbacks,
): FleetRosterStream {
  return openAuthenticatedSseConnection(
    '/api/live-instances/fleet/stream',
    'snapshot',
    {
      onStatus: callbacks.onStatus,
      onEvent: (message) => {
        try {
          const parsed: unknown = JSON.parse(message.data);
          if (!isFleetRosterSnapshot(parsed)) {
            callbacks.onMalformedSnapshot('Fleet roster stream returned an invalid snapshot.');
            return;
          }
          callbacks.onSnapshot(parsed);
        } catch (error) {
          callbacks.onMalformedSnapshot(
            `Fleet roster stream returned malformed JSON: ${
              error instanceof Error ? error.message : String(error)
            }`,
          );
        }
      },
    },
  );
}

function isLiveInstanceSummary(value: unknown): value is LiveInstanceSummary {
  if (typeof value !== 'object' || value === null) return false;
  const record = value as Record<string, unknown>;
  const verdict = record['readiness_verdict'];
  return (
    typeof record['strategy_instance_id'] === 'string' &&
    typeof record['process_state'] === 'string' &&
    (record['bound_run_id'] === undefined ||
      record['bound_run_id'] === null ||
      typeof record['bound_run_id'] === 'string') &&
    (record['latest_run_id'] === undefined ||
      record['latest_run_id'] === null ||
      typeof record['latest_run_id'] === 'string') &&
    (record['desired_state'] === undefined ||
      record['desired_state'] === null ||
      typeof record['desired_state'] === 'string') &&
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
