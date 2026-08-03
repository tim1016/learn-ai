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

function snapshot(version: number): BotPanelLiveSnapshot {
  return {
    stream_epoch: 'epoch-a',
    surface_version: version,
    panel: { revision: version } as BotPanelLiveSnapshot['panel'],
    live_chart: { resolution: '5s' } as BotPanelLiveSnapshot['live_chart'],
  };
}

describe('BotPanelLiveStore', () => {
  const originalEventSource = globalThis.EventSource;
  const service = {
    getLiveSnapshot: vi.fn().mockResolvedValue(snapshot(2)),
    liveStreamUrl: vi.fn().mockReturnValue('/api/live-stream'),
  };

  beforeEach(() => {
    vi.clearAllMocks();
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
});
