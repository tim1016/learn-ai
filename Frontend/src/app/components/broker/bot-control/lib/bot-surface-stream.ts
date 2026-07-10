import type { LiveInstanceStatus } from '../../../../api/live-instances.types';
import {
  openVersionedSnapshotStream,
  type SnapshotStream,
  type SnapshotStreamCallbacks,
} from '../../../../services/versioned-snapshot-stream';
import { isLiveInstanceStatus } from './bot-surface-snapshot-adapter';

export type BotSurfaceStream = SnapshotStream;
export type BotSurfaceStreamCallbacks = SnapshotStreamCallbacks<LiveInstanceStatus>;

export function openBotSurfaceStream(
  strategyInstanceId: string,
  callbacks: BotSurfaceStreamCallbacks,
): BotSurfaceStream {
  const encodedId = encodeURIComponent(strategyInstanceId);
  return openVersionedSnapshotStream(
    `/api/live-instances/${encodedId}/operator-surface/stream`,
    (value): value is LiveInstanceStatus => isLiveInstanceStatus(value, strategyInstanceId),
    'State stream',
    callbacks,
  );
}
