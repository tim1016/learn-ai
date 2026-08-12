import { provideHttpClient } from '@angular/common/http';
import {
  HttpTestingController,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ChartBar, ChartFillMarker } from '../../lib/broker-v2-panel.types';
import { GalleryLiveStore } from './gallery-live-store.service';
import type { GalleryBotView, GalleryLiveSnapshot, GalleryLiveUpdate } from './gallery.types';

/** Mirrors ``bot-panel-live-store.service.spec.ts``'s stub — jsdom has no ``EventSource``. */
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

function bar(startMs: number, close = '100.00'): ChartBar {
  return {
    start_ms: startMs,
    end_ms: startMs + 60_000,
    open: '100.00',
    high: '100.50',
    low: '99.50',
    close,
    volume: 1_000,
    source: 'polygon',
  };
}

function marker(orderRef: string, filledAtMs: number): ChartFillMarker {
  return {
    filled_at_ms: filledAtMs,
    side: 'buy',
    quantity: 10,
    price: 100,
    order_ref: orderRef,
  };
}

function bot(sid: string, overrides: Partial<GalleryBotView> = {}): GalleryBotView {
  return {
    sid,
    symbol: 'SPY',
    label: sid,
    running: true,
    phase: 'running',
    desired_state: 'running',
    needs_attention: false,
    realized_pnl_today: 0,
    open_pnl: 0,
    fills_today: 0,
    last_bar_at_ms: null,
    primary_action: { action_id: 'stop', label: 'Stop', enabled: true, disabled_reason: null },
    ...overrides,
  };
}

function snapshot(overrides: Partial<GalleryLiveSnapshot> = {}): GalleryLiveSnapshot {
  return {
    stream_epoch: 'epoch-a',
    surface_version: 1,
    as_of_ms: 1_700_000_000_000,
    resolution: '1m',
    bots: [bot('sid-1'), bot('sid-2', { symbol: 'QQQ' })],
    symbols: [
      { symbol: 'SPY', bars: [bar(1_000), bar(2_000)] },
      { symbol: 'QQQ', bars: [bar(1_000)] },
    ],
    markers: { 'sid-1': [marker('order-1', 1)] },
    ...overrides,
  };
}

describe('GalleryLiveStore', () => {
  const originalEventSource = globalThis.EventSource;

  beforeEach(() => {
    StubEventSource.instances = [];
    (globalThis as { EventSource?: unknown }).EventSource = StubEventSource;
    TestBed.configureTestingModule({
      providers: [GalleryLiveStore, provideHttpClient(), provideHttpClientTesting()],
    });
  });

  afterEach(() => {
    TestBed.inject(GalleryLiveStore).stop();
    TestBed.inject(HttpTestingController).verify();
    globalThis.EventSource = originalEventSource;
    vi.useRealTimers();
  });

  describe('ingest merge semantics (the jsdom-free testability seam)', () => {
    it('merges snapshot then update: bots upsert by sid, removed_sids drop, bars replace the forming bar and append', () => {
      const store = TestBed.inject(GalleryLiveStore);

      store.ingestSnapshot(snapshot());
      expect(store.bots().map((b) => b.sid)).toEqual(['sid-1', 'sid-2']);
      expect(store.barsBySymbol().get('SPY')).toEqual([bar(1_000), bar(2_000)]);

      const update: GalleryLiveUpdate = {
        surface_version: 2,
        as_of_ms: 1_700_000_060_000,
        symbols: [{ symbol: 'SPY', bars: [bar(2_000, '101.00'), bar(3_000)] }],
        markers_delta: {},
        bots_delta: [bot('sid-1', { fills_today: 1 }), bot('sid-3', { symbol: 'IWM' })],
        removed_sids: ['sid-2'],
      };
      store.ingestUpdate(update);

      expect(store.bots().map((b) => b.sid)).toEqual(['sid-1', 'sid-3']);
      expect(store.bots().find((b) => b.sid === 'sid-1')?.fills_today).toBe(1);

      const spyBars = store.barsBySymbol().get('SPY');
      expect(spyBars?.map((b) => b.start_ms)).toEqual([1_000, 2_000, 3_000]);
      expect(spyBars?.find((b) => b.start_ms === 2_000)?.close).toBe('101.00');
      // The untouched symbol is unaffected by an update that only names SPY.
      expect(store.barsBySymbol().get('QQQ')).toEqual([bar(1_000)]);
    });

    it('dedupes markers by order_ref, replacing the existing entry in place rather than duplicating', () => {
      const store = TestBed.inject(GalleryLiveStore);
      store.ingestSnapshot(
        snapshot({ markers: { 'sid-1': [marker('order-1', 1), marker('order-2', 2)] } }),
      );

      store.ingestUpdate({
        surface_version: 2,
        as_of_ms: 2,
        symbols: [],
        markers_delta: { 'sid-1': [marker('order-1', 99)] },
        bots_delta: [],
        removed_sids: [],
      });

      const markers = store.markersBySid().get('sid-1');
      expect(markers?.map((m) => m.order_ref)).toEqual(['order-1', 'order-2']);
      expect(markers?.find((m) => m.order_ref === 'order-1')?.filled_at_ms).toBe(99);
    });

    it('ignores an update whose surface_version does not advance past the last-adopted version', () => {
      const store = TestBed.inject(GalleryLiveStore);
      store.ingestSnapshot(snapshot({ surface_version: 5 }));

      store.ingestUpdate({
        surface_version: 5,
        as_of_ms: 1,
        symbols: [{ symbol: 'SPY', bars: [bar(9_999)] }],
        markers_delta: {},
        bots_delta: [bot('sid-9')],
        removed_sids: [],
      });

      expect(store.barsBySymbol().get('SPY')?.some((b) => b.start_ms === 9_999)).toBe(false);
      expect(store.bots().some((b) => b.sid === 'sid-9')).toBe(false);
    });

    it('replaces all state on a newer-epoch snapshot even carrying a lower surface_version', () => {
      const store = TestBed.inject(GalleryLiveStore);
      store.ingestSnapshot(snapshot({ surface_version: 9 }));

      store.ingestSnapshot(
        snapshot({ stream_epoch: 'epoch-b', surface_version: 1, bots: [bot('sid-9')] }),
      );

      expect(store.bots().map((b) => b.sid)).toEqual(['sid-9']);
    });
  });

  describe('EventSource lifecycle (start/stop, mirrors AccountDeskHoldingsStore + BotPanelLiveStore)', () => {
    it('bootstraps via REST, opens the stream, and routes snapshot/update frames', async () => {
      const store = TestBed.inject(GalleryLiveStore);
      const http = TestBed.inject(HttpTestingController);

      const starting = store.start('alpaca', 'PA-1');
      http.expectOne('/api/brokers/alpaca/accounts/PA-1/gallery/snapshot').flush(snapshot());
      await starting;

      expect(store.bots().map((b) => b.sid)).toEqual(['sid-1', 'sid-2']);
      const source = StubEventSource.instances[0];
      expect(source.url).toContain('/api/brokers/alpaca/accounts/PA-1/gallery/stream');

      source.emit('open');
      expect(store.status()).toBe('live');

      source.emit(
        'update',
        JSON.stringify({
          surface_version: 2,
          as_of_ms: 1,
          symbols: [],
          markers_delta: {},
          bots_delta: [bot('sid-9')],
          removed_sids: ['sid-2'],
        }),
      );
      expect(store.bots().map((b) => b.sid)).toEqual(['sid-1', 'sid-9']);

      source.emit('error');
      expect(store.status()).toBe('stale');
    });

    it('drops a malformed frame without disturbing status, and still applies the next good frame', async () => {
      const store = TestBed.inject(GalleryLiveStore);
      const http = TestBed.inject(HttpTestingController);

      const starting = store.start('alpaca', 'PA-1');
      http.expectOne('/api/brokers/alpaca/accounts/PA-1/gallery/snapshot').flush(snapshot());
      await starting;

      const source = StubEventSource.instances[0];
      source.emit('open');
      expect(store.status()).toBe('live');

      // Invalid JSON.
      source.emit('update', '{not-valid-json');
      expect(store.status()).toBe('live');

      // Valid JSON, wrong shape.
      source.emit('update', JSON.stringify({ nonsense: true }));
      expect(store.status()).toBe('live');

      // A well-formed frame right after two bad ones still applies —
      // connection health and frame-parse errors are decoupled.
      source.emit(
        'update',
        JSON.stringify({
          surface_version: 2,
          as_of_ms: 1,
          symbols: [{ symbol: 'SPY', bars: [bar(3_000)] }],
          markers_delta: {},
          bots_delta: [bot('sid-9')],
          removed_sids: [],
        }),
      );
      expect(store.status()).toBe('live');
      expect(store.bots().map((b) => b.sid)).toEqual(['sid-1', 'sid-2', 'sid-9']);
      expect(store.barsBySymbol().get('SPY')?.map((b) => b.start_ms)).toEqual([1_000, 2_000, 3_000]);
    });

    it('falls back to 5s polling while the stream is erroring and stops once it reopens', async () => {
      vi.useFakeTimers();
      const store = TestBed.inject(GalleryLiveStore);
      const http = TestBed.inject(HttpTestingController);

      const starting = store.start('alpaca', 'PA-1');
      http.expectOne('/api/brokers/alpaca/accounts/PA-1/gallery/snapshot').flush(snapshot());
      await starting;

      StubEventSource.instances[0].emit('error');
      expect(store.status()).toBe('stale');

      await vi.advanceTimersByTimeAsync(5_000);
      http
        .expectOne('/api/brokers/alpaca/accounts/PA-1/gallery/snapshot')
        .flush(snapshot({ surface_version: 2 }));

      StubEventSource.instances.at(-1)?.emit('open');
      expect(store.status()).toBe('live');

      await vi.advanceTimersByTimeAsync(10_000);
      http.expectNone('/api/brokers/alpaca/accounts/PA-1/gallery/snapshot');
    });

    it('clears prior state when starting against a different account, but not on a same-identity restart', async () => {
      const store = TestBed.inject(GalleryLiveStore);
      const http = TestBed.inject(HttpTestingController);

      const first = store.start('alpaca', 'PA-1');
      http.expectOne('/api/brokers/alpaca/accounts/PA-1/gallery/snapshot').flush(snapshot());
      await first;
      expect(store.bots().length).toBe(2);

      const second = store.start('alpaca', 'PA-2');
      // The identity change clears state synchronously, before the new
      // account's bootstrap resolves.
      expect(store.bots()).toEqual([]);
      http
        .expectOne('/api/brokers/alpaca/accounts/PA-2/gallery/snapshot')
        .flush(snapshot({ stream_epoch: 'epoch-b', bots: [bot('sid-only')] }));
      await second;
      expect(store.bots().map((b) => b.sid)).toEqual(['sid-only']);
    });

    it('preserves state across a same-identity restart instead of clearing it', async () => {
      const store = TestBed.inject(GalleryLiveStore);
      const http = TestBed.inject(HttpTestingController);

      const first = store.start('alpaca', 'PA3');
      http.expectOne('/api/brokers/alpaca/accounts/PA3/gallery/snapshot').flush(snapshot());
      await first;
      expect(store.bots().length).toBe(2);
      expect(store.barsBySymbol().get('SPY')?.length).toBe(2);

      const second = store.start('alpaca', 'PA3');
      // Unlike the identity-change case above, restarting against the
      // *same* (broker, accountId) must not blank the wall while the
      // restart's bootstrap is in flight.
      expect(store.bots().length).toBe(2);
      expect(store.barsBySymbol().get('SPY')?.length).toBe(2);

      http
        .expectOne('/api/brokers/alpaca/accounts/PA3/gallery/snapshot')
        .flush(snapshot({ surface_version: 2 }));
      await second;
      expect(store.bots().length).toBe(2);
    });
  });
});
