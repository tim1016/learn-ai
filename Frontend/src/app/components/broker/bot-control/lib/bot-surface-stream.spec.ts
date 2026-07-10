import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { environment } from '../../../../../environments/environment';
import { makeStatus } from '../bot-control-page.fixtures';
import {
  botSurfaceStreamEnabled,
  openBotSurfaceStream,
  shouldAcceptSurfaceSnapshot,
} from './bot-surface-stream';

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

describe('bot surface state stream', () => {
  beforeEach(() => {
    StubEventSource.instances = [];
    vi.stubGlobal('EventSource', StubEventSource);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    environment.flags.botCockpitStateStream = false;
  });

  it('opens the protected latest-wins snapshot channel and closes it', () => {
    const received: string[] = [];
    const stream = openBotSurfaceStream('spy bot', {
      onSnapshot: (snapshot) => received.push(snapshot.stream_epoch),
      onMalformedSnapshot: vi.fn(),
    });
    const source = StubEventSource.instances[0];
    const snapshot = makeStatus();

    source?.emit('snapshot', JSON.stringify(snapshot));
    stream.close();

    expect(source?.url).toContain('/api/live-instances/spy%20bot/operator-surface/stream');
    expect(source?.url).toContain('control_intent=learn-ai-browser-control');
    expect(received).toEqual(['fixture-epoch']);
    expect(source?.closed).toBe(true);
  });

  it('accepts higher versions and replacement epochs only', () => {
    const current = makeStatus();

    expect(
      shouldAcceptSurfaceSnapshot(current, { ...current, surface_version: 2 }),
    ).toBe(true);
    expect(
      shouldAcceptSurfaceSnapshot(current, { ...current, surface_version: 1 }),
    ).toBe(false);
    expect(
      shouldAcceptSurfaceSnapshot(current, {
        ...current,
        stream_epoch: 'replacement-epoch',
        surface_version: 1,
      }),
    ).toBe(true);
  });

  it('reads the rollout flag without requiring every local environment file to define it', () => {
    expect(botSurfaceStreamEnabled()).toBe(false);
    environment.flags.botCockpitStateStream = true;
    expect(botSurfaceStreamEnabled()).toBe(true);
  });
});
