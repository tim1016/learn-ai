import {
  DestroyRef,
  Injector,
  Signal,
  computed,
  inject,
  runInInjectionContext,
  signal,
} from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';

import { brokerSse, type SseStatus, type SseStream } from '../../../../../services/broker-sse';

import type {
  BrokerActivityPage,
  BrokerActivityRow,
} from './broker-activity.types';

const BACKFILL_PAGE_SIZE = 100;
const SSE_MAX_BUFFER = 2_000;

export interface BrokerActivityStream {
  /** Merged backfill + live rows, deduped by ``seq``, ascending. */
  rows: Signal<BrokerActivityRow[]>;
  backfillLoading: Signal<boolean>;
  backfillError: Signal<string | null>;
  sseStatus: Signal<SseStatus>;
  sseError: Signal<string | null>;
  close: () => void;
}

/**
 * Hand-rolls the cold-start backfill + SSE handoff for the broker-activity
 * surface. Lives next to the table component but factored out so both
 * the executed-trades table and the working/pending panel can subscribe
 * to a single canonical row stream per ``strategy_instance_id``.
 *
 * Must be called from an injection context — uses ``DestroyRef`` and
 * ``Injector`` to wire the SSE close hook.
 */
export function brokerActivityStream(
  strategyInstanceId: string,
): BrokerActivityStream {
  const http = inject(HttpClient);
  const injector = inject(Injector);
  const destroyRef = inject(DestroyRef);

  const backfillRows = signal<BrokerActivityRow[]>([]);
  const backfillLoading = signal<boolean>(true);
  const backfillError = signal<string | null>(null);
  const sseStream = signal<SseStream<BrokerActivityRow> | null>(null);

  const rows = computed<BrokerActivityRow[]>(() => {
    const backfill = backfillRows();
    const live = sseStream()?.data() ?? [];
    const bySeq = new Map<number, BrokerActivityRow>();
    for (const row of backfill) bySeq.set(row.seq, row);
    for (const row of live) bySeq.set(row.seq, row); // SSE wins on overlap
    return [...bySeq.values()].sort((a, b) => a.seq - b.seq);
  });

  const sseStatus = computed<SseStatus>(
    () => sseStream()?.status() ?? 'connecting',
  );
  const sseError = computed<string | null>(
    () => sseStream()?.lastError() ?? null,
  );

  let nextSeq: number | null = 0;
  let closed = false;

  const fetchPage = (afterSeq: number): Promise<BrokerActivityPage> =>
    firstValueFrom(
      http.get<BrokerActivityPage>(
        `/api/live-instances/${encodeURIComponent(strategyInstanceId)}/broker-activity`,
        { params: { after_seq: afterSeq, limit: BACKFILL_PAGE_SIZE } },
      ),
    );

  // Cold-start cycle: drain REST backfill, then open SSE. Done in an
  // async IIFE so callers see a synchronous return.
  void (async () => {
    try {
      while (nextSeq !== null && !closed) {
        const page = await fetchPage(nextSeq);
        if (page.rows.length > 0) {
          backfillRows.update((prev) => [...prev, ...page.rows]);
        }
        nextSeq = page.next_seq;
      }
      backfillLoading.set(false);
      if (closed) return;
      const stream = runInInjectionContext(injector, () =>
        brokerSse<BrokerActivityRow>(
          `/api/live-instances/${encodeURIComponent(strategyInstanceId)}/broker-activity/stream`,
          'row',
          { maxBuffer: SSE_MAX_BUFFER },
        ),
      );
      sseStream.set(stream);
    } catch (err) {
      backfillError.set(err instanceof Error ? err.message : String(err));
      backfillLoading.set(false);
    }
  })();

  const close = (): void => {
    closed = true;
    sseStream()?.close();
    sseStream.set(null);
  };

  destroyRef.onDestroy(close);

  return {
    rows,
    backfillLoading: backfillLoading.asReadonly(),
    backfillError: backfillError.asReadonly(),
    sseStatus,
    sseError,
    close,
  };
}
