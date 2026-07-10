import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { FleetRosterSnapshot } from '../api/live-instances.types';
import {
  adoptFleetRosterSnapshot,
  isFleetRosterSnapshot,
  openFleetRosterStream,
} from './fleet-roster-stream';

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

function snapshot(
  overrides: Partial<FleetRosterSnapshot> = {},
): FleetRosterSnapshot {
  return {
    stream_epoch: 'fleet-epoch',
    surface_version: 1,
    fetched_at_ms: 1_700_000_000_000,
    daemon_fetched_at_ms: 1_700_000_000_000,
    instances: [
      {
        strategy_instance_id: 'bot-a',
        process_state: 'running',
        bound_run_id: 'run-a',
        latest_run_id: 'run-a',
        desired_state: 'RUNNING',
        readiness_verdict: 'READY',
        readiness_as_of_ms: 1_700_000_000_000,
      },
    ],
    ...overrides,
  };
}

describe('fleet roster stream', () => {
  beforeEach(() => {
    StubEventSource.instances = [];
    vi.stubGlobal('EventSource', StubEventSource);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('opens the protected fleet roster channel and closes it', () => {
    const received: string[] = [];
    const stream = openFleetRosterStream({
      onSnapshot: (value) => received.push(value.stream_epoch),
      onMalformedSnapshot: vi.fn(),
      onStatus: vi.fn(),
    });
    const source = StubEventSource.instances[0];

    source?.emit('snapshot', JSON.stringify(snapshot()));
    stream.close();

    expect(source?.url).toBe(
      '/api/live-instances/fleet/stream?control_intent=learn-ai-browser-control',
    );
    expect(received).toEqual(['fleet-epoch']);
    expect(source?.closed).toBe(true);
  });

  it('accepts higher versions and replacement epochs only', () => {
    const current = snapshot();

    expect(
      adoptFleetRosterSnapshot(current, { ...current, surface_version: 2 }),
    ).toEqual({ ...current, surface_version: 2 });
    expect(
      adoptFleetRosterSnapshot(current, { ...current, surface_version: 1 }),
    ).toBe(current);
    const replacement = {
      ...current,
      stream_epoch: 'replacement-epoch',
      surface_version: 1,
    };
    expect(adoptFleetRosterSnapshot(current, replacement)).toBe(replacement);
  });

  it('rejects incomplete or malformed snapshots', () => {
    const onSnapshot = vi.fn();
    const onMalformedSnapshot = vi.fn();
    openFleetRosterStream({
      onSnapshot,
      onMalformedSnapshot,
      onStatus: vi.fn(),
    });
    const incomplete = { ...snapshot() } as Record<string, unknown>;
    delete incomplete['instances'];

    StubEventSource.instances[0].emit('snapshot', JSON.stringify(incomplete));
    StubEventSource.instances[0].emit('snapshot', '{');

    expect(onSnapshot).not.toHaveBeenCalled();
    expect(onMalformedSnapshot).toHaveBeenCalledTimes(2);
  });

  it('validates roster rows without deriving backend verdicts', () => {
    const invalidSnapshot = {
      ...snapshot(),
      instances: [
        {
          strategy_instance_id: 'bot-b',
          process_state: 'idle',
          readiness_verdict: 'NOT_A_VERDICT',
        },
      ],
    };

    expect(isFleetRosterSnapshot(snapshot())).toBe(true);
    expect(isFleetRosterSnapshot(invalidSnapshot)).toBe(false);
  });
});
