import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  adoptVersionedSnapshot,
  openVersionedSnapshotStream,
  type VersionedSnapshot,
} from './versioned-snapshot-stream';

interface TestSnapshot extends VersionedSnapshot {
  readonly fetched_at_ms: number;
}

class StubEventSource {
  static instances: StubEventSource[] = [];
  readonly listeners = new Map<string, (event: Event) => void>();
  closed = false;

  constructor(readonly url: string) {
    StubEventSource.instances.push(this);
  }

  addEventListener(name: string, listener: EventListenerOrEventListenerObject): void {
    if (typeof listener === 'function') this.listeners.set(name, listener);
  }

  emit(name: string, data: string): void {
    this.listeners.get(name)?.(new MessageEvent(name, { data }));
  }

  close(): void {
    this.closed = true;
  }
}

function isTestSnapshot(value: unknown): value is TestSnapshot {
  if (typeof value !== 'object' || value === null) return false;
  const record = value as Record<string, unknown>;
  return (
    typeof record['stream_epoch'] === 'string' &&
    record['surface_version'] === 1 &&
    record['fetched_at_ms'] === 1
  );
}

describe('versioned snapshot stream', () => {
  beforeEach(() => {
    StubEventSource.instances = [];
    vi.stubGlobal('EventSource', StubEventSource);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('accepts higher versions and replacement epochs only', () => {
    const current: TestSnapshot = {
      stream_epoch: 'epoch-a',
      surface_version: 1,
      fetched_at_ms: 1,
    };

    expect(adoptVersionedSnapshot(current, { ...current, surface_version: 2 })).toEqual({
      ...current,
      surface_version: 2,
    });
    expect(adoptVersionedSnapshot(current, { ...current, surface_version: 1 })).toBe(current);
    const replacement = {
      ...current,
      stream_epoch: 'epoch-b',
      surface_version: 1,
    };
    expect(adoptVersionedSnapshot(current, replacement)).toBe(replacement);
  });

  it('parses and validates snapshots through the shared SSE wrapper', () => {
    const onSnapshot = vi.fn();
    const onMalformedSnapshot = vi.fn();
    const stream = openVersionedSnapshotStream(
      '/api/test-snapshots/stream',
      isTestSnapshot,
      'Test stream',
      {
        onSnapshot,
        onMalformedSnapshot,
        onStatus: vi.fn(),
      },
    );
    const source = StubEventSource.instances[0];

    source.emit('snapshot', JSON.stringify({
      stream_epoch: 'epoch-a',
      surface_version: 1,
      fetched_at_ms: 1,
    }));
    source.emit('snapshot', JSON.stringify({ stream_epoch: 'epoch-a' }));
    source.emit('snapshot', '{');
    stream.close();

    expect(onSnapshot).toHaveBeenCalledWith({
      stream_epoch: 'epoch-a',
      surface_version: 1,
      fetched_at_ms: 1,
    });
    expect(onMalformedSnapshot).toHaveBeenCalledWith(
      'Test stream returned an invalid snapshot.',
    );
    expect(onMalformedSnapshot).toHaveBeenCalledWith(
      expect.stringContaining('Test stream returned malformed JSON:'),
    );
    expect(source.closed).toBe(true);
  });
});
