import { DestroyRef, type Signal, inject, signal } from '@angular/core';

import {
  openAuthenticatedSseConnection,
  type AuthenticatedSseConnection,
  type AuthenticatedSseStatus,
} from './authenticated-sse-connection';

export interface EventCursor {
  readonly durableStreamId: string;
  readonly seq: number;
}

export interface DurableEventPage<Row> {
  readonly rows: readonly Row[];
  readonly durable_stream_id: string;
  readonly high_water_cursor: string;
  readonly next_cursor: string | null;
}

export interface DurableEventFeedOptions<Row> {
  readonly streamUrl: (cursor: string) => string;
  readonly backfill: (cursor: string | null) => Promise<DurableEventPage<Row>>;
  readonly decodeRow: (value: unknown) => Row;
  readonly rowSeq: (row: Row) => number;
  readonly maxRows?: number;
}

export interface DurableEventFeed<Row> {
  readonly rows: Signal<readonly Row[]>;
  readonly status: Signal<AuthenticatedSseStatus>;
  readonly loading: Signal<boolean>;
  readonly error: Signal<string | null>;
  readonly cursor: Signal<string | null>;
  readonly close: () => void;
}

interface GapPayload {
  readonly durable_stream_id: string;
  readonly last_safe_cursor: string;
}

interface ResetPayload {
  readonly durable_stream_id: string;
}

/** Typed composite-cursor client shared by every replayable Bot Cockpit feed. */
export function durableEventFeed<Row>(
  options: DurableEventFeedOptions<Row>,
): DurableEventFeed<Row> {
  const maxRows = options.maxRows ?? 2_000;
  const rows = signal<readonly Row[]>([]);
  const status = signal<AuthenticatedSseStatus>('connecting');
  const loading = signal(true);
  const error = signal<string | null>(null);
  const cursor = signal<string | null>(null);
  let connection: AuthenticatedSseConnection | null = null;
  let closed = false;
  let generation = 0;

  const mergeRows = (incoming: readonly Row[]): void => {
    const bySeq = new Map(rows().map((row) => [options.rowSeq(row), row]));
    for (const row of incoming) {
      const seq = options.rowSeq(row);
      if (!bySeq.has(seq)) bySeq.set(seq, row);
    }
    const ordered = [...bySeq.values()].sort((a, b) => options.rowSeq(a) - options.rowSeq(b));
    rows.set(ordered.length > maxRows ? ordered.slice(-maxRows) : ordered);
  };

  const openStream = (fromCursor: string): void => {
    if (closed) return;
    connection = openAuthenticatedSseConnection(
      options.streamUrl(fromCursor),
      'row',
      {
        onStatus: (next) => {
          status.set(next);
          if (next === 'error') {
            queueMicrotask(() => void recover(cursor(), false, true));
          }
        },
        onError: (message) => {
          if (message !== null) error.set(message);
        },
        onEvent: (event) => {
          try {
            const eventCursor = parseEventCursor(event.lastEventId);
            const expected = parseEventCursor(cursor());
            if (expected !== null && eventCursor.durableStreamId !== expected.durableStreamId) {
              void recover(null, true);
              return;
            }
            const row = options.decodeRow(JSON.parse(event.data));
            if (options.rowSeq(row) !== eventCursor.seq) {
              throw new Error('Event row sequence does not match its composite cursor.');
            }
            mergeRows([row]);
            if (expected === null || eventCursor.seq > expected.seq) {
              cursor.set(event.lastEventId);
            }
            loading.set(false);
            error.set(null);
          } catch (cause) {
            error.set(errorMessage(cause));
          }
        },
        onControlEvent: (name, event) => {
          try {
            if (name === 'gap') {
              const gap = JSON.parse(event.data) as GapPayload;
              const safe = parseEventCursor(gap.last_safe_cursor);
              if (safe.durableStreamId !== gap.durable_stream_id) {
                throw new Error('Gap marker stream identity does not match its cursor.');
              }
              void recover(gap.last_safe_cursor, false);
            } else if (name === 'reset') {
              const reset = JSON.parse(event.data) as ResetPayload;
              if (!reset.durable_stream_id) throw new Error('Reset marker is missing stream identity.');
              void recover(null, true);
            } else if (name === 'end') {
              void recover(cursor(), false);
            }
          } catch (cause) {
            error.set(errorMessage(cause));
          }
        },
      },
      ['gap', 'reset', 'end'],
      { reconnect: false },
    );
  };

  const recover = async (
    fromCursor: string | null,
    resetRows: boolean,
    preserveError = false,
  ): Promise<void> => {
    const run = ++generation;
    connection?.close();
    connection = null;
    status.set('connecting');
    loading.set(true);
    if (!preserveError) error.set(null);
    if (resetRows) rows.set([]);
    try {
      let pageCursor = fromCursor;
      let highWater: string | null = fromCursor;
      while (true) {
        const page = await options.backfill(pageCursor);
        if (closed || run !== generation) return;
        const high = parseEventCursor(page.high_water_cursor);
        if (high.durableStreamId !== page.durable_stream_id) {
          throw new Error('Backfill stream identity does not match its high-water cursor.');
        }
        mergeRows(page.rows.map((row) => options.decodeRow(row)));
        highWater = page.high_water_cursor;
        if (page.next_cursor === null) break;
        const next = parseEventCursor(page.next_cursor);
        if (next.durableStreamId !== page.durable_stream_id) {
          throw new Error('Backfill next cursor changed stream identity.');
        }
        pageCursor = page.next_cursor;
      }
      if (highWater === null) throw new Error('Backfill did not provide a high-water cursor.');
      cursor.set(highWater);
      loading.set(false);
      openStream(highWater);
    } catch (cause) {
      if (closed || run !== generation) return;
      if (fromCursor !== null && isStreamReplacement(cause)) {
        void recover(null, true);
        return;
      }
      status.set('error');
      loading.set(false);
      error.set(errorMessage(cause));
    }
  };

  void recover(null, true);
  const close = (): void => {
    closed = true;
    generation += 1;
    connection?.close();
    connection = null;
    status.set('closed');
  };
  inject(DestroyRef, { optional: true })?.onDestroy(close);

  return {
    rows: rows.asReadonly(),
    status: status.asReadonly(),
    loading: loading.asReadonly(),
    error: error.asReadonly(),
    cursor: cursor.asReadonly(),
    close,
  };
}

export function parseEventCursor(value: string | null): EventCursor {
  if (value === null) throw new Error("Event cursor must be '<durable_stream_id>:<seq>'.");
  const separator = value.lastIndexOf(':');
  const durableStreamId = value.slice(0, separator);
  const rawSeq = value.slice(separator + 1);
  if (separator <= 0 || !/^\d+$/.test(rawSeq)) {
    throw new Error("Event cursor must be '<durable_stream_id>:<seq>'.");
  }
  const seq = Number(rawSeq);
  if (!Number.isSafeInteger(seq)) {
    throw new Error('Event cursor sequence exceeds the safe integer range.');
  }
  return { durableStreamId, seq };
}

function isStreamReplacement(cause: unknown): boolean {
  return Boolean(cause && typeof cause === 'object' && 'status' in cause && cause.status === 409);
}

function errorMessage(cause: unknown): string {
  return cause instanceof Error ? cause.message : String(cause);
}
