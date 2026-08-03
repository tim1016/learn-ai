import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { BotPanelLiveSnapshot } from './broker-v2-panel.types';
import { BrokerV2PanelService } from './broker-v2-panel.service';
import { BotPanelLiveStore } from './bot-panel-live-store.service';

class StubEventSource {
  static instances: StubEventSource[] = [];
  private readonly listeners = new Map<string, ((event: MessageEvent<string>) => void)[]>();

  constructor(readonly url: string) {
    StubEventSource.instances.push(this);
  }

  addEventListener(name: string, listener: EventListener): void {
    const listeners = this.listeners.get(name) ?? [];
    listeners.push(listener as (event: MessageEvent<string>) => void);
    this.listeners.set(name, listeners);
  }

  emit(name: string, data = ''): void {
    for (const listener of this.listeners.get(name) ?? []) {
      listener(new MessageEvent(name, { data }));
    }
  }

  close(): void {}
}

function snapshot(version: number, epoch = 'epoch-a'): BotPanelLiveSnapshot {
  return {
    stream_epoch: epoch,
    surface_version: version,
    panel: {
      revision: version,
      rail: { transaction_ref: null, stations: [] },
    } as unknown as BotPanelLiveSnapshot['panel'],
    live_chart: { resolution: '5s' } as BotPanelLiveSnapshot['live_chart'],
  };
}

function deferred<T>(): {
  readonly promise: Promise<T>;
  readonly resolve: (value: T) => void;
} {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((promiseResolve) => {
    resolve = promiseResolve;
  });
  return { promise, resolve };
}

describe('BotPanelLiveStore', () => {
  const originalEventSource = globalThis.EventSource;
  const service = {
    getLiveSnapshot: vi.fn().mockResolvedValue(snapshot(2)),
    getPanel: vi.fn(),
    liveStreamUrl: vi.fn().mockReturnValue('/api/live-stream'),
  };

  beforeEach(() => {
    vi.clearAllMocks();
    service.getLiveSnapshot.mockResolvedValue(snapshot(2));
    StubEventSource.instances = [];
    (globalThis as { EventSource?: unknown }).EventSource = StubEventSource;
    TestBed.configureTestingModule({
      providers: [
        BotPanelLiveStore,
        { provide: BrokerV2PanelService, useValue: service },
      ],
    });
  });

  afterEach(() => {
    TestBed.inject(BotPanelLiveStore).stop();
    globalThis.EventSource = originalEventSource;
    vi.useRealTimers();
  });

  it('keeps the last good snapshot and adopts only newer same-epoch revisions', async () => {
    const store = TestBed.inject(BotPanelLiveStore);
    await store.start({
      broker: 'alpaca',
      accountId: 'PA-1',
      sid: 'sid-1',
      resolution: '5s',
    });

    const source = StubEventSource.instances[0];
    source.emit('snapshot', JSON.stringify(snapshot(1)));
    expect(store.snapshot()?.surface_version).toBe(2);

    source.emit('snapshot', JSON.stringify(snapshot(3)));
    expect(store.snapshot()?.surface_version).toBe(3);

    source.emit('error');
    expect(store.status()).toBe('error');
    expect(store.snapshot()?.surface_version).toBe(3);
  });

  it('re-bootstraps snapshots after an epoch reset event', async () => {
    vi.useFakeTimers();
    const store = TestBed.inject(BotPanelLiveStore);
    await store.start({
      broker: 'alpaca',
      accountId: 'PA-1',
      sid: 'sid-1',
      resolution: '5s',
    });
    service.getLiveSnapshot.mockResolvedValueOnce(snapshot(1, 'epoch-b'));

    StubEventSource.instances[0].emit('reset', '{}');
    await Promise.resolve();
    await Promise.resolve();

    expect(service.getLiveSnapshot).toHaveBeenCalledTimes(2);
    expect(store.snapshot()?.stream_epoch).toBe('epoch-b');
  });

  it('polls after a stream error and stops polling when the stream reopens', async () => {
    vi.useFakeTimers();
    const store = TestBed.inject(BotPanelLiveStore);
    await store.start({
      broker: 'alpaca',
      accountId: 'PA-1',
      sid: 'sid-1',
      resolution: '5s',
    });

    StubEventSource.instances[0].emit('error');
    await vi.advanceTimersByTimeAsync(5_000);
    expect(service.getLiveSnapshot).toHaveBeenCalledTimes(2);

    StubEventSource.instances.at(-1)?.emit('open');
    await vi.advanceTimersByTimeAsync(10_000);
    expect(service.getLiveSnapshot).toHaveBeenCalledTimes(2);
  });

  it('clears the previous bot while the replacement bootstrap is pending', async () => {
    const store = TestBed.inject(BotPanelLiveStore);
    await store.start({
      broker: 'alpaca',
      accountId: 'PA-1',
      sid: 'sid-1',
      resolution: '5s',
    });
    const replacement = deferred<BotPanelLiveSnapshot>();
    service.getLiveSnapshot.mockReturnValueOnce(replacement.promise);

    const switching = store.start({
      broker: 'alpaca',
      accountId: 'PA-1',
      sid: 'sid-2',
      resolution: '5s',
    });
    expect(store.snapshot()).toBeNull();

    replacement.resolve(snapshot(1, 'epoch-b'));
    await switching;
    expect(store.snapshot()?.stream_epoch).toBe('epoch-b');
  });

  it('ignores a refresh response that resolves after switching bots', async () => {
    const store = TestBed.inject(BotPanelLiveStore);
    await store.start({
      broker: 'alpaca',
      accountId: 'PA-1',
      sid: 'sid-1',
      resolution: '5s',
    });
    const stale = deferred<BotPanelLiveSnapshot>();
    service.getLiveSnapshot.mockReturnValueOnce(stale.promise);
    const refreshing = store.refresh();
    service.getLiveSnapshot.mockResolvedValueOnce(snapshot(1, 'epoch-b'));

    await store.start({
      broker: 'alpaca',
      accountId: 'PA-1',
      sid: 'sid-2',
      resolution: '5s',
    });
    stale.resolve(snapshot(99, 'epoch-a'));
    await refreshing;

    expect(store.snapshot()?.stream_epoch).toBe('epoch-b');
  });

  it('ignores transaction evidence that resolves after switching bots', async () => {
    const store = TestBed.inject(BotPanelLiveStore);
    await store.start({
      broker: 'alpaca',
      accountId: 'PA-1',
      sid: 'sid-1',
      resolution: '5s',
    });
    const stalePanel = deferred<BotPanelLiveSnapshot['panel']>();
    service.getPanel.mockReturnValueOnce(stalePanel.promise);
    const selecting = store.selectTransaction('tx-old');
    service.getLiveSnapshot.mockResolvedValueOnce(snapshot(1, 'epoch-b'));

    await store.start({
      broker: 'alpaca',
      accountId: 'PA-1',
      sid: 'sid-2',
      resolution: '5s',
    });
    stalePanel.resolve({
      rail: { transaction_ref: 'tx-old', stations: [] },
    } as unknown as BotPanelLiveSnapshot['panel']);
    await selecting;

    expect(store.snapshot()?.panel.rail.transaction_ref).toBeNull();
  });
});
