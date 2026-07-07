import {
  Injector,
  Signal,
  computed,
  inject,
  runInInjectionContext,
  signal,
} from '@angular/core';

import type { BotEventRow } from '../../../../../api/live-runs.types';
import { brokerSse, type SseStatus, type SseStream } from '../../../../../services/broker-sse';

const SSE_MAX_BUFFER = 2_000;

export interface BotEventRowStream {
  /** Merged authored rows from SSE backfill + live delivery, deduped by seq. */
  rows: Signal<BotEventRow[]>;
  isLoading: Signal<boolean>;
  errorMessage: Signal<string | null>;
  sseStatus: Signal<SseStatus>;
  close: () => void;
}

/**
 * Subscribes to one run-scoped Bot event stream. The backend replays rows
 * with ``seq > since_seq`` on this same SSE channel before polling for live
 * rows, so the UI does not need a separate REST paging loop.
 */
export function botEventRowStream(runId: string): BotEventRowStream {
  const injector = inject(Injector);
  const sseStream = signal<SseStream<BotEventRow> | null>(null);

  const rows = computed<BotEventRow[]>(() => {
    const live = sseStream()?.data() ?? [];
    const bySeq = new Map<number, BotEventRow>();
    for (const row of live) bySeq.set(row.seq, row);
    return [...bySeq.values()].sort((a, b) => a.seq - b.seq);
  });

  const sseStatus = computed<SseStatus>(() => sseStream()?.status() ?? 'connecting');
  const errorMessage = computed<string | null>(() => sseStream()?.lastError() ?? null);
  const isLoading = computed<boolean>(() => sseStatus() === 'connecting');

  const url = `/api/live-runs/${encodeURIComponent(runId)}/bot-events/stream?since_seq=0`;
  const stream = runInInjectionContext(injector, () =>
    brokerSse<BotEventRow>(url, 'row', { maxBuffer: SSE_MAX_BUFFER }),
  );
  sseStream.set(stream);

  const close = (): void => {
    sseStream()?.close();
    sseStream.set(null);
  };

  return {
    rows,
    isLoading,
    errorMessage,
    sseStatus,
    close,
  };
}
