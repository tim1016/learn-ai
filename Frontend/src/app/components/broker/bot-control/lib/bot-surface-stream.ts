import type { LiveInstanceStatus } from '../../../../api/live-instances.types';
import type { AuthenticatedSseStatus } from '../../../../services/authenticated-sse-connection';
import { openAuthenticatedSseConnection } from '../../../../services/authenticated-sse-connection';
import { isLiveInstanceStatus } from './bot-surface-snapshot-adapter';

export interface BotSurfaceStream {
  close: () => void;
}

export interface BotSurfaceStreamCallbacks {
  onSnapshot: (snapshot: LiveInstanceStatus) => void;
  onMalformedSnapshot: (message: string) => void;
  onStatus: (status: AuthenticatedSseStatus) => void;
}

export function openBotSurfaceStream(
  strategyInstanceId: string,
  callbacks: BotSurfaceStreamCallbacks,
): BotSurfaceStream {
  const encodedId = encodeURIComponent(strategyInstanceId);
  return openAuthenticatedSseConnection(
    `/api/live-instances/${encodedId}/operator-surface/stream`,
    'snapshot',
    {
      onStatus: callbacks.onStatus,
      onEvent: (message) => {
        try {
          const parsed: unknown = JSON.parse(message.data);
          if (!isLiveInstanceStatus(parsed, strategyInstanceId)) {
            callbacks.onMalformedSnapshot('State stream returned an invalid snapshot.');
            return;
          }
          callbacks.onSnapshot(parsed);
        } catch (error) {
          callbacks.onMalformedSnapshot(
            `State stream returned malformed JSON: ${error instanceof Error ? error.message : String(error)}`,
          );
        }
      },
    },
  );
}
