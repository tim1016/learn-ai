import { Injector, runInInjectionContext } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { durableEventFeed, parseEventCursor } from './durable-event-feed';

interface Row {
  readonly seq: number;
  readonly value: string;
}

class StubEventSource {
  static instances: StubEventSource[] = [];
  readonly listeners = new Map<string, ((event: Event) => void)[]>();
  closed = false;

  constructor(readonly url: string) {
    StubEventSource.instances.push(this);
  }

  addEventListener(name: string, listener: (event: Event) => void): void {
    this.listeners.set(name, [...(this.listeners.get(name) ?? []), listener]);
  }

  dispatch(name: string, data: unknown, lastEventId = ''): void {
    const event = new MessageEvent(name, {
      data: typeof data === 'string' ? data : JSON.stringify(data),
      lastEventId,
    });
    for (const listener of this.listeners.get(name) ?? []) listener(event);
  }

  close(): void {
    this.closed = true;
  }
}

describe('durableEventFeed', () => {
  beforeEach(() => {
    StubEventSource.instances = [];
    vi.stubGlobal('EventSource', StubEventSource);
    TestBed.configureTestingModule({ providers: [] });
  });

  afterEach(() => {
    TestBed.resetTestingModule();
    vi.unstubAllGlobals();
  });

  it('pages REST backfill, resumes at a composite high-water cursor, and dedupes by stable seq', async () => {
    const backfill = vi.fn()
      .mockResolvedValueOnce(page('stream-a', 1, [row(1, 'first')], 'stream-a:1'))
      .mockResolvedValueOnce(page('stream-a', 2, [row(2, 'second')], null));
    const feed = setup(backfill);
    await flushAsync();

    expect(backfill.mock.calls.map(([cursor]) => cursor)).toEqual([null, 'stream-a:1']);
    expect(StubEventSource.instances[0].url).toContain('cursor=stream-a%3A2');

    StubEventSource.instances[0].dispatch('row', row(2, 'revised'), 'stream-a:2');
    StubEventSource.instances[0].dispatch('row', row(3, 'third'), 'stream-a:3');

    expect(feed.rows()).toEqual([row(1, 'first'), row(2, 'second'), row(3, 'third')]);
    expect(feed.cursor()).toBe('stream-a:3');
  });

  it('uses the gap marker safe cursor for deep backfill before reopening', async () => {
    const backfill = vi.fn()
      .mockResolvedValueOnce(page('stream-a', 2, [row(1, 'first'), row(2, 'second')], null))
      .mockResolvedValueOnce(page('stream-a', 3, [row(3, 'third')], null));
    const feed = setup(backfill);
    await flushAsync();
    const first = StubEventSource.instances[0];

    first.dispatch('gap', {
      durable_stream_id: 'stream-a',
      last_safe_cursor: 'stream-a:2',
    });
    await flushAsync();

    expect(first.closed).toBe(true);
    expect(backfill).toHaveBeenLastCalledWith('stream-a:2');
    expect(StubEventSource.instances[1].url).toContain('cursor=stream-a%3A3');
    expect(feed.rows()).toEqual([row(1, 'first'), row(2, 'second'), row(3, 'third')]);
  });

  it('reconnects through REST from the latest acknowledged cursor', async () => {
    const backfill = vi.fn()
      .mockResolvedValueOnce(page('stream-a', 1, [row(1, 'first')], null))
      .mockResolvedValueOnce(page('stream-a', 2, [], null));
    const feed = setup(backfill);
    await flushAsync();
    const first = StubEventSource.instances[0];
    first.dispatch('row', row(2, 'second'), 'stream-a:2');

    first.dispatch('error', '');
    await flushAsync();

    expect(backfill).toHaveBeenLastCalledWith('stream-a:2');
    expect(StubEventSource.instances[1].url).toContain('cursor=stream-a%3A2');
    expect(feed.rows()).toEqual([row(1, 'first'), row(2, 'second')]);
  });

  it('clears the old sequence namespace and re-bootstraps after reset', async () => {
    const backfill = vi.fn()
      .mockResolvedValueOnce(page('stream-a', 1, [row(1, 'old')], null))
      .mockResolvedValueOnce(page('stream-b', 1, [row(1, 'replacement')], null));
    const feed = setup(backfill);
    await flushAsync();

    StubEventSource.instances[0].dispatch('reset', { durable_stream_id: 'stream-b' });
    await flushAsync();

    expect(backfill).toHaveBeenLastCalledWith(null);
    expect(feed.rows()).toEqual([row(1, 'replacement')]);
    expect(feed.cursor()).toBe('stream-b:1');
  });

  it('rejects a malformed channel row before admitting typed history', async () => {
    const feed = setup(vi.fn().mockResolvedValue(page('stream-a', 0, [], null)));
    await flushAsync();

    StubEventSource.instances[0].dispatch('row', { seq: 1 }, 'stream-a:1');

    expect(feed.rows()).toEqual([]);
    expect(feed.error()).toBe('Invalid test row.');
  });
});

describe('parseEventCursor', () => {
  it('keeps stream identity separate from sequence', () => {
    expect(parseEventCursor('stream:with:colons:42')).toEqual({
      durableStreamId: 'stream:with:colons',
      seq: 42,
    });
    expect(() => parseEventCursor('stream-only')).toThrow(/durable_stream_id/);
  });
});

function setup(backfill: (cursor: string | null) => Promise<ReturnType<typeof page>>) {
  const injector = TestBed.inject(Injector);
  return runInInjectionContext(injector, () => durableEventFeed<Row>({
    backfill,
    decodeRow: (value) => {
      if (
        typeof value !== 'object' ||
        value === null ||
        !Number.isSafeInteger((value as { seq?: unknown }).seq) ||
        typeof (value as { value?: unknown }).value !== 'string'
      ) {
        throw new Error('Invalid test row.');
      }
      return value as Row;
    },
    rowSeq: (value) => value.seq,
    streamUrl: (cursor) => `/events?cursor=${encodeURIComponent(cursor)}`,
  }));
}

function page(
  stream: string,
  highWaterSeq: number,
  rows: readonly Row[],
  nextCursor: string | null,
) {
  return {
    rows,
    durable_stream_id: stream,
    high_water_cursor: `${stream}:${highWaterSeq}`,
    next_cursor: nextCursor,
  };
}

function row(seq: number, value: string): Row {
  return { seq, value };
}

async function flushAsync(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}
