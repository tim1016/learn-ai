import { Injector, runInInjectionContext } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import {
  HttpTestingController,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';

import { brokerActivityStream } from './broker-activity-stream';
import type { BrokerActivityPage, BrokerActivityRow } from './broker-activity.types';

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
  TestBed.configureTestingModule({
    providers: [provideHttpClient(), provideHttpClientTesting()],
  });
  const injector = TestBed.inject(Injector);
  const http = TestBed.inject(HttpTestingController);
  const stream = runInInjectionContext(injector, () =>
    brokerActivityStream(strategyInstanceId),
  );
  return { stream, http, injector };
}

afterEach(() => TestBed.resetTestingModule());

describe('brokerActivityStream', () => {
  it('cold-starts with a REST backfill paginated by after_seq', async () => {
    const { stream, http } = setup('sid-1');

    const req = http.expectOne(
      '/api/live-instances/sid-1/broker-activity?after_seq=0&limit=100',
    );
    expect(req.request.method).toBe('GET');
    const page: BrokerActivityPage = {
      rows: [row({ seq: 1 }), row({ seq: 2 })],
      next_seq: null,
    };
    req.flush(page);

    // Microtask drain so the next-step setup runs (no SSE expected since we returned null next_seq).
    await Promise.resolve();
    await Promise.resolve();

    expect(stream.rows().map((r) => r.seq)).toEqual([1, 2]);
    expect(stream.backfillLoading()).toBe(false);

    // SSE opens once backfill drains.
    expect(eventSources.length).toBe(1);
    expect(eventSources[0].url).toBe(
      '/api/live-instances/sid-1/broker-activity/stream',
    );
  });

  it('pages REST until next_seq is null, then switches to SSE', async () => {
    const { stream, http } = setup('sid-2');

    const r1 = http.expectOne(
      '/api/live-instances/sid-2/broker-activity?after_seq=0&limit=100',
    );
    r1.flush({ rows: [row({ seq: 1 })], next_seq: 2 } satisfies BrokerActivityPage);
    await Promise.resolve();
    await Promise.resolve();

    const r2 = http.expectOne(
      '/api/live-instances/sid-2/broker-activity?after_seq=2&limit=100',
    );
    r2.flush({ rows: [row({ seq: 3 })], next_seq: null } satisfies BrokerActivityPage);
    await Promise.resolve();
    await Promise.resolve();

    expect(stream.rows().map((r) => r.seq)).toEqual([1, 3]);
    expect(eventSources.length).toBe(1);
  });

  it('merges SSE rows on top of the backfill, deduping by seq (SSE wins)', async () => {
    const { stream, http } = setup('sid-3');

    const r1 = http.expectOne(
      '/api/live-instances/sid-3/broker-activity?after_seq=0&limit=100',
    );
    r1.flush({
      rows: [row({ seq: 5, symbol: 'OLD' })],
      next_seq: null,
    } satisfies BrokerActivityPage);
    await Promise.resolve();
    await Promise.resolve();

    expect(eventSources.length).toBe(1);
    const source = eventSources[0];

    // Live update that re-asserts seq=5 (publisher might have re-authored)
    // and adds a new seq=6.
    source.dispatch('row', JSON.stringify(row({ seq: 5, symbol: 'NEW' })));
    source.dispatch('row', JSON.stringify(row({ seq: 6, symbol: 'AAPL' })));

    const seqs = stream.rows().map((r) => ({ seq: r.seq, symbol: r.symbol }));
    expect(seqs).toEqual([
      { seq: 5, symbol: 'NEW' }, // SSE wins on overlap
      { seq: 6, symbol: 'AAPL' },
    ]);
  });

  it('records a backfill error when the REST call fails', async () => {
    const { stream, http } = setup('sid-err');

    const req = http.expectOne(
      '/api/live-instances/sid-err/broker-activity?after_seq=0&limit=100',
    );
    req.flush('boom', { status: 500, statusText: 'Server error' });
    await Promise.resolve();
    await Promise.resolve();

    expect(stream.backfillLoading()).toBe(false);
    expect(stream.backfillError()).not.toBeNull();
    // SSE is not opened when backfill fails.
    expect(eventSources.length).toBe(0);
  });
});
