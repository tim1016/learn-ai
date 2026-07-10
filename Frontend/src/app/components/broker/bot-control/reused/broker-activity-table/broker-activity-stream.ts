import { HttpClient, HttpParams } from '@angular/common/http';
import { type Signal, computed, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import type { SseStatus } from '../../../../../services/broker-sse';
import { durableEventFeed } from '../../../../../services/durable-event-feed';
import type { BrokerActivityPage, BrokerActivityRow } from './broker-activity.types';

const SSE_MAX_BUFFER = 2_000;

export interface BrokerActivityStream {
  rows: Signal<readonly BrokerActivityRow[]>;
  backfillLoading: Signal<boolean>;
  backfillError: Signal<string | null>;
  sseStatus: Signal<SseStatus>;
  sseError: Signal<string | null>;
  close: () => void;
}

/** Composite-cursor backfill plus live delivery for one bot's broker activity. */
export function brokerActivityStream(strategyInstanceId: string): BrokerActivityStream {
  const http = inject(HttpClient);
  const base = `/api/live-instances/${encodeURIComponent(strategyInstanceId)}/broker-activity`;
  const feed = durableEventFeed<BrokerActivityRow>({
    maxRows: SSE_MAX_BUFFER,
    rowSeq: (row) => row.seq,
    decodeRow: decodeBrokerActivityRow,
    backfill: (cursor) => {
      let params = new HttpParams().set('limit', '500');
      if (cursor !== null) params = params.set('cursor', cursor);
      return firstValueFrom(http.get<BrokerActivityPage>(base, { params }));
    },
    streamUrl: (cursor) => `${base}/stream?cursor=${encodeURIComponent(cursor)}`,
  });

  return {
    rows: feed.rows,
    backfillLoading: feed.loading,
    backfillError: feed.error,
    sseStatus: computed(() => feed.status()),
    sseError: feed.error,
    close: feed.close,
  };
}

function decodeBrokerActivityRow(value: unknown): BrokerActivityRow {
  if (typeof value !== 'object' || value === null) throw new Error('Invalid broker-activity row.');
  const row = value as Record<string, unknown>;
  if (
    !Number.isSafeInteger(row['seq']) ||
    !Number.isSafeInteger(row['ts_ms']) ||
    typeof row['symbol'] !== 'string' ||
    typeof row['headline'] !== 'string' ||
    typeof row['narrative'] !== 'string' ||
    !Array.isArray(row['reason_codes'])
  ) {
    throw new Error('Invalid broker-activity row.');
  }
  return value as BrokerActivityRow;
}
