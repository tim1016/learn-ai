import type { LiveInstanceStatus } from '../../../../api/live-instances.types';
import { withDataPlaneControlIntent } from '../../../../services/broker-sse';
import { environment } from '../../../../../environments/environment';

export interface BotSurfaceStream {
  close: () => void;
}

export interface BotSurfaceStreamCallbacks {
  onSnapshot: (snapshot: LiveInstanceStatus) => void;
  onMalformedSnapshot: (message: string) => void;
}

export function botSurfaceStreamEnabled(): boolean {
  return (
    'botCockpitStateStream' in environment.flags &&
    environment.flags.botCockpitStateStream === true
  );
}

export function openBotSurfaceStream(
  strategyInstanceId: string,
  callbacks: BotSurfaceStreamCallbacks,
): BotSurfaceStream {
  const encodedId = encodeURIComponent(strategyInstanceId);
  const url = withDataPlaneControlIntent(
    `/api/live-instances/${encodedId}/operator-surface/stream`,
  );
  const source = new EventSource(url);
  source.addEventListener('snapshot', (event: Event) => {
    const message = event as MessageEvent<string>;
    try {
      const parsed: unknown = JSON.parse(message.data);
      if (!isLiveInstanceStatus(parsed)) {
        callbacks.onMalformedSnapshot('State stream returned an invalid snapshot.');
        return;
      }
      callbacks.onSnapshot(parsed);
    } catch (error) {
      callbacks.onMalformedSnapshot(
        `State stream returned malformed JSON: ${error instanceof Error ? error.message : String(error)}`,
      );
    }
  });
  return { close: () => source.close() };
}

export function shouldAcceptSurfaceSnapshot(
  current: LiveInstanceStatus | null,
  candidate: LiveInstanceStatus,
): boolean {
  if (current === null || current.stream_epoch !== candidate.stream_epoch) return true;
  return candidate.surface_version > current.surface_version;
}

function isLiveInstanceStatus(value: unknown): value is LiveInstanceStatus {
  if (typeof value !== 'object' || value === null) return false;
  const record = value as Record<string, unknown>;
  return (
    typeof record['strategy_instance_id'] === 'string' &&
    typeof record['stream_epoch'] === 'string' &&
    typeof record['surface_version'] === 'number' &&
    typeof record['operator_surface'] === 'object' &&
    record['operator_surface'] !== null
  );
}
