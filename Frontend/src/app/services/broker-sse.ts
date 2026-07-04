import { DestroyRef, Signal, computed, inject, signal } from '@angular/core';

import {
  DATA_PLANE_CONTROL_INTENT_QUERY,
  DATA_PLANE_CONTROL_INTENT_VALUE,
} from '../security/data-plane-control-intent.interceptor';

/**
 * Reactive wrapper over an SSE ``EventSource`` for the IBKR broker
 * streams.
 *
 * Each broker SSE endpoint emits a named event (``chain``, ``pnl``,
 * ``order``) plus an ``error`` event for server-side BrokerError. This
 * helper:
 *
 * * Subscribes to the named event and accumulates payloads into a
 *   bounded signal.
 * * Exposes a ``status`` signal so the UI can render the connection
 *   state distinctly.
 * * Handles both the named ``error`` event (server BrokerError) and the
 *   underlying transport ``error`` (browser auto-reconnect).
 * * Closes the underlying ``EventSource`` on the calling injection
 *   context's ``DestroyRef`` — this is what triggers the IBKR-side
 *   ``cancelMktData`` / ``cancelPnL`` cleanup.
 *
 * Must be called from within an injection context (component
 * constructor or factory) for the destroy hook to fire.
 */

export type SseStatus = 'connecting' | 'open' | 'closed' | 'error';

export interface SseStream<T> {
  data: Signal<readonly T[]>;
  latest: Signal<T | null>;
  status: Signal<SseStatus>;
  lastError: Signal<string | null>;
  clear: () => void;
  close: () => void;
}

export interface BrokerSseOptions {
  /** Maximum number of payloads to retain. Older entries are dropped FIFO. Default 1000. */
  maxBuffer?: number;
  /** Marks native EventSource requests that must receive the private data-plane secret at the dev proxy. */
  dataPlaneControlIntent?: boolean;
}

const DEFAULT_MAX_BUFFER = 1000;

export function brokerSse<T>(
  url: string,
  eventName: string,
  options: BrokerSseOptions = {},
): SseStream<T> {
  const data = signal<T[]>([]);
  const status = signal<SseStatus>('connecting');
  const lastError = signal<string | null>(null);
  const maxBuffer = options.maxBuffer ?? DEFAULT_MAX_BUFFER;

  const source = new EventSource(
    options.dataPlaneControlIntent ? withDataPlaneControlIntent(url) : url,
  );

  source.addEventListener('open', () => {
    status.set('open');
    lastError.set(null);
  });

  source.addEventListener(eventName, (e: Event) => {
    const messageEvent = e as MessageEvent<string>;
    let payload: T;
    try {
      payload = JSON.parse(messageEvent.data) as T;
    } catch (parseError) {
      lastError.set(
        `Malformed SSE payload on '${eventName}': ${(parseError as Error).message}`,
      );
      return;
    }
    data.update((prev) => {
      const next = [...prev, payload];
      return next.length > maxBuffer ? next.slice(next.length - maxBuffer) : next;
    });
  });

  // Server-side BrokerError is delivered as ``event: error`` with a
  // JSON ``{"error": "..."}`` payload. Distinguish from transport
  // errors (no ``data`` field) by checking the cast type.
  source.addEventListener('error', (e: Event) => {
    const messageEvent = e as MessageEvent<string>;
    if (messageEvent.data) {
      try {
        const parsed = JSON.parse(messageEvent.data) as { error?: string };
        lastError.set(parsed.error ?? 'Unknown broker error');
      } catch {
        lastError.set(messageEvent.data);
      }
      status.set('error');
    } else {
      // Transport-level error. The browser auto-reconnects every ~3s
      // by default; we keep the buffer and flip status until the next
      // event arrives (the ``open`` listener clears it).
      status.set('error');
    }
  });

  // Clean up on host destroy. Without this the IBKR-side stream would
  // leak its server-side line until the EventSource was GC'd.
  const destroyRef = inject(DestroyRef, { optional: true });
  destroyRef?.onDestroy(() => {
    source.close();
  });

  const close = () => {
    source.close();
    status.set('closed');
  };
  const clear = () => {
    data.set([]);
  };

  return {
    data: data.asReadonly(),
    latest: computed(() => {
      const arr = data();
      return arr.length === 0 ? null : (arr[arr.length - 1] ?? null);
    }),
    status: status.asReadonly(),
    lastError: lastError.asReadonly(),
    clear,
    close,
  };
}

export function withDataPlaneControlIntent(url: string): string {
  const hashIndex = url.indexOf('#');
  const base = hashIndex < 0 ? url : url.slice(0, hashIndex);
  const hash = hashIndex < 0 ? '' : url.slice(hashIndex);
  const separator = base.includes('?') ? '&' : '?';
  return `${base}${separator}${encodeURIComponent(DATA_PLANE_CONTROL_INTENT_QUERY)}=${encodeURIComponent(DATA_PLANE_CONTROL_INTENT_VALUE)}${hash}`;
}
