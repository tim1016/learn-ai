import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { makeStatus } from '../bot-control-page.fixtures';
import { openBotSurfaceStream } from './bot-surface-stream';
import { adoptBotSurfaceSnapshot } from './bot-surface-snapshot-adapter';

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
  });

  it('opens the protected latest-wins snapshot channel and closes it', () => {
    const received: string[] = [];
    const stream = openBotSurfaceStream('spy bot', {
      onSnapshot: (snapshot) => received.push(snapshot.stream_epoch),
      onMalformedSnapshot: vi.fn(),
      onStatus: vi.fn(),
    });
    const source = StubEventSource.instances[0];
    const snapshot = makeStatus({ id: 'spy bot' });

    source?.emit('snapshot', JSON.stringify(snapshot));
    stream.close();

    expect(source?.url).toBe(
      '/api/live-instances/spy%20bot/operator-surface/stream?control_intent=learn-ai-browser-control',
    );
    expect(received).toEqual(['fixture-epoch']);
    expect(source?.closed).toBe(true);
  });

  it('accepts higher versions and replacement epochs only', () => {
    const current = makeStatus();

    expect(
      adoptBotSurfaceSnapshot(current, { ...current, surface_version: 2 }),
    ).toEqual({ ...current, surface_version: 2 });
    expect(
      adoptBotSurfaceSnapshot(current, { ...current, surface_version: 1 }),
    ).toBe(current);
    const replacement = {
      ...current,
      stream_epoch: 'replacement-epoch',
      surface_version: 1,
    };
    expect(
      adoptBotSurfaceSnapshot(current, replacement),
    ).toBe(replacement);
  });

  it('rejects a well-formed snapshot for a different route identity', () => {
    const onSnapshot = vi.fn();
    const onMalformedSnapshot = vi.fn();
    openBotSurfaceStream('sid-a', {
      onSnapshot,
      onMalformedSnapshot,
      onStatus: vi.fn(),
    });

    StubEventSource.instances[0].emit(
      'snapshot',
      JSON.stringify(makeStatus({ id: 'sid-b' })),
    );

    expect(onSnapshot).not.toHaveBeenCalled();
    expect(onMalformedSnapshot).toHaveBeenCalledWith(
      'State stream returned an invalid snapshot.',
    );
  });

  it('rejects an incomplete snapshot before the store can dereference it', () => {
    const onSnapshot = vi.fn();
    const onMalformedSnapshot = vi.fn();
    openBotSurfaceStream('sid-x', {
      onSnapshot,
      onMalformedSnapshot,
      onStatus: vi.fn(),
    });
    const incomplete = { ...makeStatus() } as Record<string, unknown>;
    delete incomplete['latest_mutation'];

    StubEventSource.instances[0].emit('snapshot', JSON.stringify(incomplete));

    expect(onSnapshot).not.toHaveBeenCalled();
    expect(onMalformedSnapshot).toHaveBeenCalledOnce();
  });
});
