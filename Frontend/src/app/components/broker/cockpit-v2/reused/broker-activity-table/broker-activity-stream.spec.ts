import {
  Injector,
  effect,
  runInInjectionContext,
  signal,
} from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';

import { brokerActivityStream } from './broker-activity-stream';
import type { BrokerActivityRow } from './broker-activity.types';

// Tracked instances so a test can grab the most recent EventSource and
// fire named events at it. Matches the broker-orders.spec stub pattern.
const eventSources: StubEventSource[] = [];

class StubEventSource {
  readonly listeners = new Map<string, ((ev: Event) => unknown)[]>();
  readonly url: string;
  closed = false;

  constructor(url: string) {
    this.url = url;
    eventSources.push(this);
  }
  addEventListener(name: string, fn: (ev: Event) => unknown): void {
    const arr = this.listeners.get(name) ?? [];
    arr.push(fn);
    this.listeners.set(name, arr);
  }
  removeEventListener(name: string, fn: (ev: Event) => unknown): void {
    const arr = this.listeners.get(name) ?? [];
    this.listeners.set(name, arr.filter((f) => f !== fn));
  }
  dispatch(name: string, data?: string): void {
    const ev = new MessageEvent(name, { data: data ?? '' }) as unknown as Event;
    for (const fn of this.listeners.get(name) ?? []) fn(ev);
  }
  close(): void {
    this.closed = true;
  }
}

beforeAll(() => {
  (globalThis as { EventSource?: unknown }).EventSource = StubEventSource;
});

beforeEach(() => {
  eventSources.length = 0;
});

function row(overrides: Partial<BrokerActivityRow> = {}): BrokerActivityRow {
  return {
    seq: 1,
    ts_ms: 1_700_000_000_000,
    exec_id: 'exec-1',
    perm_id: 9001,
    order_ref: 'ref-1',
    symbol: 'SPY',
    side: 'BUY',
    quantity: 1,
    price: 100,
    commission: 0.5,
    net_amount: -100.5,
    order_type: 'MKT',
    exec_ts_ms: 1_700_000_000_500,
    verdict: 'expected',
    template_key: 'normal_fill_v1',
    template_version: 1,
    headline: 'BUY 1 SPY @ 100',
    narrative: 'ok',
    reason_codes: [],
    engine_overlay: null,
    divergence_facts: null,
    ...overrides,
  };
}

function setup(strategyInstanceId: string) {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({ providers: [] });
  const injector = TestBed.inject(Injector);
  const stream = runInInjectionContext(injector, () =>
    brokerActivityStream(strategyInstanceId),
  );
  return { stream, injector };
}

afterEach(() => TestBed.resetTestingModule());

describe('brokerActivityStream', () => {
  it('opens the SSE channel with since_seq=0 on cold start (no REST paging)', () => {
    setup('sid-1');

    // Exactly one EventSource, opened with since_seq=0 — backend backfills
    // on the same channel, so no REST paging precedes it.
    expect(eventSources.length).toBe(1);
    expect(eventSources[0].url).toBe(
      '/api/live-instances/sid-1/broker-activity/stream?since_seq=0',
    );
  });

  it('renders backfill rows received on the SSE channel in seq order', () => {
    const { stream } = setup('sid-2');

    const source = eventSources[0];
    // Simulate the backend's server-side backfill: rows arrive on the
    // ``row`` event before any live publisher event.
    source.dispatch('row', JSON.stringify(row({ seq: 1, symbol: 'AAA' })));
    source.dispatch('row', JSON.stringify(row({ seq: 2, symbol: 'BBB' })));

    expect(stream.rows().map((r) => ({ seq: r.seq, symbol: r.symbol }))).toEqual([
      { seq: 1, symbol: 'AAA' },
      { seq: 2, symbol: 'BBB' },
    ]);
  });

  it('dedups overlapping seq across backfill + live (later event wins)', () => {
    const { stream } = setup('sid-3');

    const source = eventSources[0];
    // Backfill row (server replay) then a live re-author of the same seq
    // — the backend dedups by seq <= last_emitted on its side, but the
    // frontend's dedup map is the canonical guarantee from the operator's
    // point of view.
    source.dispatch('row', JSON.stringify(row({ seq: 5, symbol: 'OLD' })));
    source.dispatch('row', JSON.stringify(row({ seq: 5, symbol: 'NEW' })));
    source.dispatch('row', JSON.stringify(row({ seq: 6, symbol: 'AAPL' })));

    expect(stream.rows().map((r) => ({ seq: r.seq, symbol: r.symbol }))).toEqual([
      { seq: 5, symbol: 'NEW' },
      { seq: 6, symbol: 'AAPL' },
    ]);
  });

  it('surfaces SSE error payloads via sseError', () => {
    const { stream } = setup('sid-err');

    const source = eventSources[0];
    source.dispatch('error', JSON.stringify({ error: 'publisher unavailable' }));

    expect(stream.sseError()).toBe('publisher unavailable');
  });

  it('reports backfillLoading=true until the SSE channel transitions to open', () => {
    const { stream } = setup('sid-loading');

    // Before any ``open`` event the helper reports ``connecting``, so the
    // operator-facing "Loading history…" surface should be true.
    expect(stream.backfillLoading()).toBe(true);

    const source = eventSources[0];
    source.dispatch('open');

    expect(stream.backfillLoading()).toBe(false);
  });

  it('closes the underlying EventSource on close()', () => {
    const { stream } = setup('sid-close');

    const source = eventSources[0];
    expect(source.closed).toBe(false);

    stream.close();

    expect(source.closed).toBe(true);
  });

  // Regression for the activity-tab effect feedback-loop bug: the effect
  // that owns the stream MUST only depend on ``strategy_instance_id`` (the
  // input), never on the stream signal it writes to. Reading the stream
  // signal inside the effect — to close the previous one — re-runs the
  // effect on every write, closing and reopening the SSE connection on
  // each turn. The shape below mirrors the fixed effect: read the sid,
  // write the stream signal via ``runInInjectionContext`` + the factory,
  // tear down on cleanup.
  it('does not feedback-loop: one stream per strategy_instance_id change', () => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({ providers: [] });
    const injector = TestBed.inject(Injector);

    const sid = signal('sid-once');
    const streamSignal = signal<ReturnType<typeof brokerActivityStream> | null>(
      null,
    );

    runInInjectionContext(injector, () => {
      effect((onCleanup) => {
        const current = sid();
        const next = runInInjectionContext(injector, () =>
          brokerActivityStream(current),
        );
        streamSignal.set(next);
        onCleanup(() => next.close());
      });
    });

    // Let the initial effect run.
    TestBed.flushEffects();
    // Read the stream signal (simulates a template binding) — this must
    // NOT cause the effect to re-fire.
    void streamSignal();
    TestBed.flushEffects();
    void streamSignal();
    TestBed.flushEffects();

    expect(eventSources.length).toBe(1);
  });
});
