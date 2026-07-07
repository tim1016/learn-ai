import { provideZonelessChangeDetection } from '@angular/core';
import { fireEvent, render, screen, waitFor } from '@testing-library/angular';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';

import type { BotEventRow } from '../../../../../api/live-runs.types';
import { BotEventStreamComponent } from './bot-event-stream.component';

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

  dispatch(name: string, data?: string): void {
    const event = new MessageEvent(name, { data: data ?? '' }) as unknown as Event;
    for (const listener of this.listeners.get(name) ?? []) listener(event);
  }

  close(): void {
    this.closed = true;
  }
}

const ROW: BotEventRow = {
  schema_version: 1,
  seq: 7,
  ts_ms: 1_700_000_000_000,
  event_type: 'order_rejected',
  source_authority: 'broker_session',
  identity: {
    evaluation_id: 'eval-1',
    intent_id: 'intent-1',
    order_ref: 'learn-ai/bot-a/v1:intent-1',
    req_id: 42,
    order_id: 100,
    perm_id: 200,
    exec_id: null,
  },
  severity: 'critical',
  headline: 'IBKR rejected the order',
  narrative: 'Order rejected - insufficient buying power',
  gate_steps: [
    {
      evaluation_id: 'eval-1',
      gate_id: 'broker.place_order',
      gate_result: 'block',
      source_authority: 'broker_session',
      facts: { reason_code: 'INSUFFICIENT_BUYING_POWER' },
    },
  ],
  terminal_error: {
    code: 'order_rejected',
    source: 'ibkr',
    gate_id: 'broker.place_order',
    message: 'IBKR order rejected',
    detail: null,
    external_code: 201,
    external_message: 'Order rejected - insufficient buying power',
    cause_chain: [],
    forensic_facts: { external_code: 201, reason_code: 'INSUFFICIENT_BUYING_POWER' },
  },
  facts: { raw_event_types: ['order_rejected'], order_ref: 'learn-ai/bot-a/v1:intent-1' },
};

describe('BotEventStreamComponent', () => {
  beforeAll(() => {
    originalEventSource = globalThis.EventSource;
    globalThis.EventSource = StubEventSource as unknown as typeof EventSource;
  });

  beforeEach(() => {
    eventSources.length = 0;
  });

  afterEach(() => {
    for (const source of eventSources) source.close();
  });

  afterAll(() => {
    globalThis.EventSource = originalEventSource as typeof EventSource;
  });

  it('subscribes to SSE and renders authored bot event rows', async () => {
    await render(BotEventStreamComponent, {
      inputs: { runId: 'run-1' },
      providers: [provideZonelessChangeDetection()],
    });
    await waitFor(() => expect(eventSources.length).toBe(1));
    expect(eventSources[0].url).toBe(
      '/api/live-runs/run-1/bot-events/stream?since_seq=0',
    );

    emitRow(ROW);
    await screen.findByText('IBKR rejected the order');
    expect(screen.getByText('Order rejected - insufficient buying power')).toBeTruthy();
    expect(screen.getByText('Order Rejected')).toBeTruthy();
    expect(screen.getByText('Broker Session')).toBeTruthy();
  });

  it('expands gate and terminal evidence', async () => {
    await render(BotEventStreamComponent, {
      inputs: { runId: 'run-1' },
      providers: [provideZonelessChangeDetection()],
    });
    await waitFor(() => expect(eventSources.length).toBe(1));
    emitRow(ROW);
    await screen.findByText('IBKR rejected the order');

    fireEvent.click(screen.getByRole('button', { name: 'Toggle bot event row 7' }));

    await waitFor(() => {
      expect(screen.getAllByText('Broker Place Order').length).toBeGreaterThan(0);
    });
    expect(screen.getAllByText('Insufficient Buying Power').length).toBeGreaterThan(0);
    expect(screen.getAllByText('learn-ai/bot-a/v1:intent-1').length).toBeGreaterThan(0);
    expect(screen.getAllByText('201').length).toBeGreaterThan(0);
  });

  it('renders empty state without a run id', async () => {
    await render(BotEventStreamComponent, {
      inputs: { runId: null },
      providers: [provideZonelessChangeDetection()],
    });

    await screen.findByTestId('bot-event-stream-empty');
    expect(screen.getByText('No bot events yet for this run.')).toBeTruthy();
    expect(eventSources.length).toBe(0);
  });

  it('renders server-side SSE errors', async () => {
    await render(BotEventStreamComponent, {
      inputs: { runId: 'run-1' },
      providers: [provideZonelessChangeDetection()],
    });
    await waitFor(() => expect(eventSources.length).toBe(1));

    eventSources[0].dispatch('error', JSON.stringify({ error: 'stream unavailable' }));

    await screen.findByTestId('bot-event-stream-error');
    expect(screen.getByText('Bot event stream unavailable: stream unavailable')).toBeTruthy();
  });
});

function emitRow(row: BotEventRow): void {
  eventSources[0].dispatch('row', JSON.stringify(row));
}
