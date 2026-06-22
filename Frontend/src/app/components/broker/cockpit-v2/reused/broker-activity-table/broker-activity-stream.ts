import {
  Injector,
  Signal,
  computed,
  inject,
  runInInjectionContext,
  signal,
} from '@angular/core';

import { brokerSse, type SseStatus, type SseStream } from '../../../../../services/broker-sse';

import type { BrokerActivityRow } from './broker-activity.types';

const SSE_MAX_BUFFER = 2_000;

export interface BrokerActivityStream {
  /** Merged rows from the SSE channel, deduped by ``seq``, ascending. */
  rows: Signal<BrokerActivityRow[]>;
  /**
   * ``true`` until the SSE handshake transitions to ``open``. Operator-facing
   * "Loading history…" surface — once the channel is open the backend's
   * server-side backfill (rows with ``seq > since_seq``) flows in as the
   * first batch of ``row`` events, so "open" is the honest moment we have
   * something to show.
   */
  backfillLoading: Signal<boolean>;
  /**
   * Kept on the public surface for binding stability (the table component
   * has a separate error pane for backfill vs live errors). The SSE-only
   * stream surfaces every error via ``sseError``; this is always ``null``.
   */
  backfillError: Signal<string | null>;
  sseStatus: Signal<SseStatus>;
  sseError: Signal<string | null>;
  close: () => void;
}

/**
 * Subscribes the broker-activity SSE channel for a single
 * ``strategy_instance_id``. The backend (ADR 0014 amendment) backfills
 * rows with ``seq > since_seq`` on the SSE channel itself before
 * forwarding live publisher events, so the frontend doesn't need a
 * separate REST paging loop — eliminating the gap window where a row
 * authored between "last REST page returned ``next_seq=null``" and "SSE
 * subscription registered" would be lost.
 *
 * Must be called from an injection context — ``brokerSse`` uses
 * ``DestroyRef`` to close the underlying ``EventSource`` on host destroy.
 *
 * Reconnect: the browser auto-reconnects to the same URL
 * (``since_seq=0``) so the backend replays the full backlog; the
 * seq-keyed dedup map in ``rows()`` absorbs the overlap so no duplicate
 * row surfaces to the UI. A cursor-aware close-and-reopen reconnect
 * (driven by the highest seq seen) is left for a follow-up; today's
 * naive reconnect is correct, just chattier than necessary.
 */
export function brokerActivityStream(
  strategyInstanceId: string,
): BrokerActivityStream {
  const injector = inject(Injector);

  const sseStream = signal<SseStream<BrokerActivityRow> | null>(null);

  const rows = computed<BrokerActivityRow[]>(() => {
    const live = sseStream()?.data() ?? [];
    const bySeq = new Map<number, BrokerActivityRow>();
    // Later events with the same seq supersede earlier ones (publisher
    // may re-author a row before the operator has acted on it).
    for (const row of live) bySeq.set(row.seq, row);
    return [...bySeq.values()].sort((a, b) => a.seq - b.seq);
  });

  const sseStatus = computed<SseStatus>(
    () => sseStream()?.status() ?? 'connecting',
  );
  const sseError = computed<string | null>(
    () => sseStream()?.lastError() ?? null,
  );
  // The "loading history" hint stays true until the channel is open
  // (at which point the server-side backfill is on its way).
  const backfillLoading = computed<boolean>(() => sseStatus() !== 'open');
  // No separate REST surface, so no separate REST error.
  const backfillError = signal<string | null>(null);

  const url =
    `/api/live-instances/${encodeURIComponent(strategyInstanceId)}` +
    `/broker-activity/stream?since_seq=0`;
  const stream = runInInjectionContext(injector, () =>
    brokerSse<BrokerActivityRow>(url, 'row', { maxBuffer: SSE_MAX_BUFFER }),
  );
  sseStream.set(stream);

  const close = (): void => {
    sseStream()?.close();
    sseStream.set(null);
  };

  return {
    rows,
    backfillLoading,
    backfillError: backfillError.asReadonly(),
    sseStatus,
    sseError,
    close,
  };
}
