import { Injector, runInInjectionContext } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import type { BotEventRow } from '../../../../../api/live-runs.types';
import { LiveRunsService } from '../../../../../services/live-runs.service';
import { botEventRowStream } from './bot-event-row-stream';

const eventSources: StubEventSource[] = [];
let originalEventSource: unknown;

class StubEventSource {
  readonly listeners = new Map<string, ((ev: Event) => unknown)[]>();
  readonly url: string;
  closed = false;

  constructor(url: string) {
    this.url = url;
    eventSources.push(this);
  }

  addEventListener(name: string, fn: (ev: Event) => unknown): void {
    const listeners = this.listeners.get(name) ?? [];
    listeners.push(fn);
    this.listeners.set(name, listeners);
  }

  removeEventListener(name: string, fn: (ev: Event) => unknown): void {
    const listeners = this.listeners.get(name) ?? [];
    this.listeners.set(name, listeners.filter((listener) => listener !== fn));
  }

  dispatch(name: string, data?: string, lastEventId = ''): void {
    const event = new MessageEvent(name, { data: data ?? '', lastEventId }) as unknown as Event;
    for (const listener of this.listeners.get(name) ?? []) listener(event);
  }

  close(): void {
    this.closed = true;
  }
}

beforeAll(() => {
  originalEventSource = globalThis.EventSource;
  vi.stubGlobal('EventSource', StubEventSource);
});

beforeEach(() => {
  eventSources.length = 0;
});

afterEach(() => {
  for (const source of eventSources) source.close();
  TestBed.resetTestingModule();
});

afterAll(() => {
  vi.stubGlobal('EventSource', originalEventSource);
});

describe('botEventRowStream', () => {
  it('opens the run-scoped bot-event SSE channel from the REST high-water cursor', async () => {
    await setup('run/with space');

    expect(eventSources.length).toBe(1);
    expect(eventSources[0].url).toBe(
      '/api/live-runs/run%2Fwith%20space/bot-events/stream?cursor=stream-a%3A0&control_intent=learn-ai-browser-control',
    );
  });

  it('dedupes rows by stable seq with the latest row winning', async () => {
    const stream = await setup('run-1');
    const source = eventSources[0];

    source.dispatch('row', JSON.stringify(row({ seq: 2, headline: 'old' })), 'stream-a:2');
    source.dispatch('row', JSON.stringify(row({ seq: 1, headline: 'first' })), 'stream-a:1');
    source.dispatch('row', JSON.stringify(row({ seq: 2, headline: 'new' })), 'stream-a:2');

    expect(stream.rows().map((item) => [item.seq, item.headline])).toEqual([
      [1, 'first'],
      [2, 'old'],
    ]);
  });

  it('surfaces server-side SSE errors', async () => {
    const stream = await setup('run-err');

    eventSources[0].dispatch('error', JSON.stringify({ error: 'stream unavailable' }));

    expect(stream.errorMessage()).toBe('stream unavailable');
  });

  it('closes the underlying EventSource', async () => {
    const stream = await setup('run-close');
    const source = eventSources[0];

    stream.close();

    expect(source.closed).toBe(true);
  });
});

async function setup(runId: string) {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [{
      provide: LiveRunsService,
      useValue: {
        getBotEvents: () => Promise.resolve({
          rows: [],
          next_seq: null,
          durable_stream_id: 'stream-a',
          high_water_cursor: 'stream-a:0',
          next_cursor: null,
        }),
      },
    }],
  });
  const injector = TestBed.inject(Injector);
  const stream = runInInjectionContext(injector, () => botEventRowStream(runId));
  await Promise.resolve();
  await Promise.resolve();
  return stream;
}

function row(overrides: Partial<BotEventRow> = {}): BotEventRow {
  return {
    schema_version: 1,
    seq: 1,
    ts_ms: 1_700_000_000_000,
    event_type: 'signal_fired',
    source_authority: 'engine_loop',
    identity: {
      evaluation_id: 'eval-1',
      intent_id: null,
      order_ref: null,
      req_id: null,
      order_id: null,
      perm_id: null,
      exec_id: null,
    },
    severity: 'info',
    headline: 'Signal fired',
    narrative: 'The strategy decided to act on this evaluation.',
    gate_steps: [],
    terminal_error: null,
    facts: {},
    ...overrides,
  };
}
