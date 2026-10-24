import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { describe, expect, it, vi } from 'vitest';

import { BackfillJobRunner, type BackfillJobRun, type BackfillTerminalEvent } from './backfill-job-runner';
import { DataLakeService } from './data-lake.service';
import { TradeBackfillRunner, type TradeBackfillEnd } from './trade-backfill-runner';
import type { TradeBackfillWindow } from './trade-backfill-spec';

const WINDOW: TradeBackfillWindow = {
  symbol: 'SPY', price_adjustment_mode: 'raw', start_trading_date_ms: Date.UTC(2024, 2, 4, 12), end_trading_date_ms: Date.UTC(2024, 2, 15, 12),
};

/** The runner over a job whose observation ends as `terminal` says, with the data plane's backfill defaults answering. */
function runnerOver(terminal: BackfillTerminalEvent) {
  const job = {
    jobId: 'job-1',
    state: signal({ kind: 'running', started: true, progress: null }),
    terminal: Promise.resolve(terminal),
    detach: vi.fn(),
    cancel: vi.fn(),
  } as unknown as BackfillJobRun;
  const defaults = { market: 'usa', lean_image_digest: 'sha256:test', max_trading_range_days: 1830, max_symbol_length: 5, provider_history_start_ms: 0 };
  TestBed.configureTestingModule({
    providers: [
      { provide: DataLakeService, useValue: { backfillDefaults: vi.fn().mockResolvedValue({ kind: 'ok', value: defaults }) } },
      { provide: BackfillJobRunner, useValue: { start: vi.fn().mockResolvedValue(job) } },
    ],
  });
  return TestBed.inject(TradeBackfillRunner);
}

describe('TradeBackfillRunner', () => {
  it.each<[string, BackfillTerminalEvent, TradeBackfillEnd]>([
    ['completes', { kind: 'completed' }, { kind: 'completed', failure: null }],
    ['fails with its own code', { kind: 'failed', code: 'provider_rate_limited', message: 'Too many requests' }, { kind: 'failed', code: 'provider_rate_limited', message: 'Too many requests' }],
    ['is cancelled, which is its own end and not a failure', { kind: 'cancelled' }, { kind: 'cancelled', code: 'backfill_cancelled', message: 'The backfill was cancelled.' }],
    ['is left unobserved', { kind: 'detached' }, { kind: 'detached' }],
  ])('ends as the job’s terminal says when it %s', async (_name, terminal, expected) => {
    const end = await runnerOver(terminal).run(() => WINDOW, () => true);

    expect(end).toEqual(expected);
  });
});
