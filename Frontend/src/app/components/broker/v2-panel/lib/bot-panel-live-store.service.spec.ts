import { HttpErrorResponse } from '@angular/common/http';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type {
  BotPanelLiveSnapshot,
  LiveSnapshotUnavailableDetail,
} from './broker-v2-panel.types';
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

const STALL: LiveSnapshotUnavailableDetail = {
  reason: 'PRODUCER_STALLED',
  message: 'The live panel stopped updating.',
  why: 'The data plane has not completed a panel refresh in over 20 seconds.',
  next_action: 'Do not act on the last values shown.',
  last_produced_at_ms: 1_700_000_000_000,
  observed_at_ms: 1_700_000_060_000,
};

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
      clerkId: 'clrk_spec',
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

  it('marks the snapshot stale on a stream stale event and clears it on the next frame (#2353)', async () => {
    const store = TestBed.inject(BotPanelLiveStore);
    await store.start({
      broker: 'alpaca',
      clerkId: 'clrk_spec',
      accountId: 'PA-1',
      sid: 'sid-1',
      resolution: '5s',
    });
    const source = StubEventSource.instances[0];
    source.emit('open');
    expect(store.stall()).toBeNull();

    source.emit('stale', JSON.stringify(STALL));
    expect(store.stall()).toEqual(STALL);
    expect(store.status()).toBe('open');

    // The recovered producer republishes the same version; it still ends the stall.
    source.emit('snapshot', JSON.stringify(snapshot(2)));
    expect(store.stall()).toBeNull();
    expect(store.snapshot()?.surface_version).toBe(2);
  });

  it('adopts a typed stalled-producer refusal from REST instead of the frozen snapshot (#2353)', async () => {
    const store = TestBed.inject(BotPanelLiveStore);
    await store.start({
      broker: 'alpaca',
      clerkId: 'clrk_spec',
      accountId: 'PA-1',
      sid: 'sid-1',
      resolution: '5s',
    });
    service.getLiveSnapshot.mockRejectedValueOnce(
      new HttpErrorResponse({ status: 503, error: { detail: STALL } }),
    );

    await store.refresh();

    expect(store.stall()).toEqual(STALL);
    await store.refresh();
    expect(store.stall()).toBeNull();
  });

  it('loads the frozen panel under the stall when opened mid-stall (#2353 review)', async () => {
    service.getLiveSnapshot.mockRejectedValueOnce(
      new HttpErrorResponse({ status: 503, error: { detail: STALL } }),
    );
    const store = TestBed.inject(BotPanelLiveStore);
    await store.start({
      broker: 'alpaca',
      clerkId: 'clrk_spec',
      accountId: 'PA-1',
      sid: 'sid-1',
      resolution: '5s',
    });
    expect(store.snapshot()).toBeNull();

    // The stream's explicitly stale bootstrap: the frozen frame, then the stall.
    const source = StubEventSource.instances[0];
    source.emit('snapshot', JSON.stringify(snapshot(2)));
    source.emit('stale', JSON.stringify(STALL));

    expect(store.snapshot()?.surface_version).toBe(2);
    expect(store.stall()).toEqual(STALL);
  });

  it('does not reinstate a stall from a REST 503 that resolves after the recovery frame (#2353 review)', async () => {
    const store = TestBed.inject(BotPanelLiveStore);
    await store.start({
      broker: 'alpaca',
      clerkId: 'clrk_spec',
      accountId: 'PA-1',
      sid: 'sid-1',
      resolution: '5s',
    });
    const source = StubEventSource.instances[0];
    source.emit('stale', JSON.stringify(STALL));
    let reject!: (error: unknown) => void;
    service.getLiveSnapshot.mockReturnValueOnce(
      new Promise<BotPanelLiveSnapshot>((_resolve, promiseReject) => {
        reject = promiseReject;
      }),
    );
    const refreshing = store.refresh();

    // Recovery arrives on the stream before the in-flight 503 resolves.
    source.emit('snapshot', JSON.stringify(snapshot(2)));
    reject(new HttpErrorResponse({ status: 503, error: { detail: STALL } }));
    await refreshing;

    expect(store.stall()).toBeNull();
  });

  it('does not clear a stall with a pre-stall REST 200 that resolves after the stale frame (#2353 review)', async () => {
    const store = TestBed.inject(BotPanelLiveStore);
    await store.start({
      broker: 'alpaca',
      clerkId: 'clrk_spec',
      accountId: 'PA-1',
      sid: 'sid-1',
      resolution: '5s',
    });
    const source = StubEventSource.instances[0];
    const late = deferred<BotPanelLiveSnapshot>();
    service.getLiveSnapshot.mockReturnValueOnce(late.promise);
    const refreshing = store.refresh();

    source.emit('stale', JSON.stringify(STALL));
    late.resolve(snapshot(2));
    await refreshing;

    expect(store.stall()).toEqual(STALL);
  });

  it('reports a malformed stale event instead of ignoring it', async () => {
    const store = TestBed.inject(BotPanelLiveStore);
    await store.start({
      broker: 'alpaca',
      clerkId: 'clrk_spec',
      accountId: 'PA-1',
      sid: 'sid-1',
      resolution: '5s',
    });

    StubEventSource.instances[0].emit('stale', '{"reason":"PRODUCER_STALLED"}');

    expect(store.error()).toBe('Bot panel stream returned an invalid stale notice.');
  });

  it('re-bootstraps snapshots after an epoch reset event', async () => {
    vi.useFakeTimers();
    const store = TestBed.inject(BotPanelLiveStore);
    await store.start({
      broker: 'alpaca',
      clerkId: 'clrk_spec',
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
      clerkId: 'clrk_spec',
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
      clerkId: 'clrk_spec',
      accountId: 'PA-1',
      sid: 'sid-1',
      resolution: '5s',
    });
    const replacement = deferred<BotPanelLiveSnapshot>();
    service.getLiveSnapshot.mockReturnValueOnce(replacement.promise);

    const switching = store.start({
      broker: 'alpaca',
      clerkId: 'clrk_spec',
      accountId: 'PA-1',
      sid: 'sid-2',
      resolution: '5s',
    });
    expect(store.snapshot()).toBeNull();

    replacement.resolve(snapshot(1, 'epoch-b'));
    await switching;
    expect(store.snapshot()?.stream_epoch).toBe('epoch-b');
  });

  it('clears and rejects old stream frames when the same account is rebound', async () => {
    const store = TestBed.inject(BotPanelLiveStore);
    await store.start({
      broker: 'alpaca',
      clerkId: 'clrk_spec',
      accountId: 'PA-1',
      sid: 'sid-1',
      resolution: '5s',
      bindingGeneration: 1,
      routingEpoch: 1,
    });
    const oldSource = StubEventSource.instances[0];
    const replacement = deferred<BotPanelLiveSnapshot>();
    service.getLiveSnapshot.mockReturnValueOnce(replacement.promise);

    const rebinding = store.start({
      broker: 'alpaca',
      clerkId: 'clrk_spec',
      accountId: 'PA-1',
      sid: 'sid-1',
      resolution: '5s',
      bindingGeneration: 2,
      routingEpoch: 2,
    });
    expect(store.snapshot()).toBeNull();

    // A browser can deliver an event queued just before close. The callback
    // belongs to the superseded generation and must not resurrect its panel.
    oldSource.emit('snapshot', JSON.stringify(snapshot(99, 'epoch-old')));
    expect(store.snapshot()).toBeNull();

    replacement.resolve(snapshot(1, 'epoch-new'));
    await rebinding;
    expect(store.snapshot()?.stream_epoch).toBe('epoch-new');
    expect(service.getLiveSnapshot).toHaveBeenLastCalledWith(
      expect.objectContaining({ bindingGeneration: 2, routingEpoch: 2 }),
      'sid-1',
      '5s',
    );
  });

  it('ignores a refresh response that resolves after switching bots', async () => {
    const store = TestBed.inject(BotPanelLiveStore);
    await store.start({
      broker: 'alpaca',
      clerkId: 'clrk_spec',
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
      clerkId: 'clrk_spec',
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
      clerkId: 'clrk_spec',
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
      clerkId: 'clrk_spec',
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

  it('keeps the latest transaction when selections resolve out of order', async () => {
    const store = TestBed.inject(BotPanelLiveStore);
    await store.start({
      broker: 'alpaca',
      clerkId: 'clrk_spec',
      accountId: 'PA-1',
      sid: 'sid-1',
      resolution: '5s',
    });
    const first = deferred<BotPanelLiveSnapshot['panel']>();
    const second = deferred<BotPanelLiveSnapshot['panel']>();
    service.getPanel
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);

    const selectingFirst = store.selectTransaction('tx-first');
    const selectingSecond = store.selectTransaction('tx-second');
    second.resolve({
      rail: { transaction_ref: 'tx-second', stations: [] },
    } as unknown as BotPanelLiveSnapshot['panel']);
    await selectingSecond;
    first.resolve({
      rail: { transaction_ref: 'tx-first', stations: [] },
    } as unknown as BotPanelLiveSnapshot['panel']);
    await selectingFirst;

    expect(store.snapshot()?.panel.rail.transaction_ref).toBe('tx-second');
  });
});
