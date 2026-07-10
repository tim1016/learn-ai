import type { AuthenticatedSseStatus } from './authenticated-sse-connection';
import { openAuthenticatedSseConnection } from './authenticated-sse-connection';

export interface VersionedSnapshot {
  readonly stream_epoch: string;
  readonly surface_version: number;
}

export interface SnapshotStream {
  readonly close: () => void;
}

export interface SnapshotStreamCallbacks<T extends VersionedSnapshot> {
  readonly onSnapshot: (snapshot: T) => void;
  readonly onMalformedSnapshot: (message: string) => void;
  readonly onStatus: (status: AuthenticatedSseStatus) => void;
}

/** ADR-0028 latest-wins adoption: new epochs replace, same-epoch versions advance. */
export function adoptVersionedSnapshot<T extends VersionedSnapshot>(
  current: T | null,
  candidate: T,
): T {
  if (current === null || current.stream_epoch !== candidate.stream_epoch) return candidate;
  return candidate.surface_version > current.surface_version ? candidate : current;
}

export function openVersionedSnapshotStream<T extends VersionedSnapshot>(
  url: string,
  isValidSnapshot: (value: unknown) => value is T,
  label: string,
  callbacks: SnapshotStreamCallbacks<T>,
): SnapshotStream {
  return openAuthenticatedSseConnection(url, 'snapshot', {
    onStatus: callbacks.onStatus,
    onEvent: (message) => {
      try {
        const parsed: unknown = JSON.parse(message.data);
        if (!isValidSnapshot(parsed)) {
          callbacks.onMalformedSnapshot(`${label} returned an invalid snapshot.`);
          return;
        }
        callbacks.onSnapshot(parsed);
      } catch (error) {
        callbacks.onMalformedSnapshot(
          `${label} returned malformed JSON: ${error instanceof Error ? error.message : String(error)}`,
        );
      }
    },
  });
}
