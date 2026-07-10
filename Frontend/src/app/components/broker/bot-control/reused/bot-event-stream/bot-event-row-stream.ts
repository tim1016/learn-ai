import { type Signal, computed, inject } from '@angular/core';

import type { BotEventRow } from '../../../../../api/live-runs.types';
import { durableEventFeed } from '../../../../../services/durable-event-feed';
import { LiveRunsService } from '../../../../../services/live-runs.service';
import type { SseStatus } from '../../../../../services/broker-sse';

const SSE_MAX_BUFFER = 2_000;

export interface BotEventRowStream {
  rows: Signal<readonly BotEventRow[]>;
  isLoading: Signal<boolean>;
  errorMessage: Signal<string | null>;
  sseStatus: Signal<SseStatus>;
  close: () => void;
}

/** Composite-cursor backfill plus live delivery for one run's authored events. */
export function botEventRowStream(runId: string): BotEventRowStream {
  const liveRuns = inject(LiveRunsService);
  const base = `/api/live-runs/${encodeURIComponent(runId)}/bot-events`;
  const feed = durableEventFeed<BotEventRow>({
    maxRows: SSE_MAX_BUFFER,
    rowSeq: (row) => row.seq,
    decodeRow: decodeBotEventRow,
    backfill: async (cursor) => {
      const page = await liveRuns.getBotEvents(runId, {
        ...(cursor === null ? {} : { cursor }),
        limit: 500,
      });
      return page;
    },
    streamUrl: (cursor) => `${base}/stream?cursor=${encodeURIComponent(cursor)}`,
  });

  return {
    rows: feed.rows,
    isLoading: feed.loading,
    errorMessage: feed.error,
    sseStatus: computed(() => feed.status()),
    close: feed.close,
  };
}

function decodeBotEventRow(value: unknown): BotEventRow {
  if (typeof value !== 'object' || value === null) throw new Error('Invalid bot-event row.');
  const row = value as Record<string, unknown>;
  if (
    !Number.isSafeInteger(row['seq']) ||
    typeof row['headline'] !== 'string' ||
    typeof row['narrative'] !== 'string' ||
    typeof row['severity'] !== 'string' ||
    typeof row['identity'] !== 'object' ||
    row['identity'] === null ||
    !Array.isArray(row['gate_steps']) ||
    typeof row['facts'] !== 'object' ||
    row['facts'] === null
  ) {
    throw new Error('Invalid bot-event row.');
  }
  return value as BotEventRow;
}
