import { TestBed } from '@angular/core/testing';
import { describe, it, expect } from 'vitest';

import { JobsService, type JobStreamEvent } from '../../services/jobs.service';
import { DataLakeService, tradingRangeRejection, tradingRangeSpanDays } from '../data-lake';
import type { BackfillDefaults, DataLakeRead } from '../data-lake';
import { EnsureCoverageService, fitBackfillWindow } from './ensure-coverage.service';
import { etIsoDate } from '../date/et-midnight';
import { fakeTickerCatalog, provideFakeTickerCatalog } from '../ticker-catalog/testing/fake-ticker-catalog';
import type { FakeTickerCatalog } from '../ticker-catalog/testing/fake-ticker-catalog';
import type { TickerOption } from '../ticker-range-picker/ticker-range-picker.types';

const DEFAULTS: BackfillDefaults = {
  market: 'usa',
  lean_image_digest: 'digest@sha256:deadbeef',
  max_trading_range_days: 1830,
  max_symbol_length: 20,
};

const SPY: TickerOption = {
  symbol: 'SPY',
  name: 'SPDR S&P 500 ETF',
  exchange: 'ARCA',
  firstHeld: '2024-05-20',
  lastHeld: '2026-09-04',
};

interface FakeJobs {
  readonly started: { type: string; payload: Record<string, unknown> }[];
  readonly cancelled: string[];
  startError: Error | null;
  /** When set, `startJob` awaits this instead of resolving immediately. */
  holdStart: Promise<string> | null;
  emit(jobId: string, event: JobStreamEvent): void;
}

function configureService(
  options: { defaults?: DataLakeRead<BackfillDefaults> } = {},
): { service: EnsureCoverageService; jobs: FakeJobs; catalog: FakeTickerCatalog } {
  const started: FakeJobs['started'] = [];
  const cancelled: string[] = [];
  // The gate registers its fold through onEvent after startJob resolves, so
  // emitting goes through whichever handler is registered for the job id.
  const handlers = new Map<string, (event: JobStreamEvent) => void>();
  const jobs: FakeJobs = {
    started,
    cancelled,
    startError: null,
    holdStart: null,
    emit(jobId, event) {
      handlers.get(jobId)?.(event);
    },
  };
  const catalog = fakeTickerCatalog([SPY]);
  let startSeq = 0;
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      provideFakeTickerCatalog(catalog),
      {
        provide: JobsService,
        useValue: {
          startJob: async (type: string, payload: Record<string, unknown>) => {
            if (jobs.startError !== null) throw jobs.startError;
            if (jobs.holdStart !== null) await jobs.holdStart;
            started.push({ type, payload });
            startSeq += 1;
            const id = `job-${startSeq}`;
            handlers.set(id, () => undefined);
            return id;
          },
          onEvent: (id: string, handler: (event: JobStreamEvent) => void) => {
            handlers.set(id, handler);
            return () => handlers.delete(id);
          },
          cancelJob: async (id: string) => {
            cancelled.push(id);
          },
        },
      },
      {
        provide: DataLakeService,
        useValue: {
          backfillDefaults: async () => options.defaults ?? { kind: 'ok', value: DEFAULTS },
        },
      },
    ],
  });
  return { service: TestBed.inject(EnsureCoverageService), jobs, catalog };
}

async function flush(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0));
}

describe('EnsureCoverageService', () => {
  it('answers a held symbol immediately, without starting a job', async () => {
    const { service, jobs } = configureService();

    const ready = await service.ensure('SPY', 'polygon_split_adjusted');

    expect(ready).toBe(true);
    expect(jobs.started).toEqual([]);
    expect(service.active()).toBeNull();
  });

  it('backfills an unheld symbol with a full-history trade-bar spec', async () => {
    const { service, jobs, catalog } = configureService();

    const run = service.ensure('NVDA', 'polygon_split_adjusted');
    await flush();

    expect(jobs.started).toHaveLength(1);
    expect(jobs.started[0].type).toBe('data_lake_backfill');
    const spec = jobs.started[0].payload['spec'] as Record<string, unknown>;
    expect(spec['symbols']).toEqual(['NVDA']);
    expect(spec['data_types']).toEqual(['trade']);
    expect(spec['price_adjustment_mode']).toBe('polygon_split_adjusted');
    expect(spec['market']).toBe('usa');
    expect(typeof spec['lean_image_digest']).toBe('string');
    expect(spec['start_trading_date_ms']).toBeLessThan(spec['end_trading_date_ms'] as number);
    // The composed window must fit the cap the data plane enforces — the
    // reviewer's bug had a naive `today - cap` start land on a 422 every
    // weekday, because the inclusive span plus weekday walk-backs reached
    // 1831–1833 days. Assert the same invariant the backend applies.
    const startIso = etIsoDate(spec['start_trading_date_ms'] as number);
    const endIso = etIsoDate(spec['end_trading_date_ms'] as number);
    expect(
      tradingRangeRejection(startIso, endIso, DEFAULTS.max_trading_range_days),
    ).toBeNull();
    // In flight: the gate names the symbol and carries no failure.
    expect(service.active()).toMatchObject({ symbol: 'NVDA', phase: 'backfilling' });

    // The job completes and the lake now holds the symbol.
    catalog.view.pool.set([
      ...catalog.view.pool(),
      { symbol: 'NVDA', name: 'NVIDIA', lastHeld: '2026-09-04' },
    ]);
    jobs.emit('job-1', { type: 'job.completed' });
    const ready = await run;

    expect(ready).toBe(true);
    expect(service.active()).toBeNull();
    // Completion re-read the catalog so every picker sees the new coverage.
    expect(catalog.view.reloadCount).toBeGreaterThan(0);
  });

  it('shows progress ticks while the backfill runs', async () => {
    const { service, jobs, catalog } = configureService();

    const run = service.ensure('NVDA', 'polygon_split_adjusted');
    await flush();
    jobs.emit('job-1', { type: 'job.progress', current: 250, total: 1000 });
    await flush();

    expect(service.active()).toMatchObject({ phase: 'backfilling', percent: 25 });

    jobs.emit('job-1', { type: 'job.completed' });
    catalog.view.pool.set([...catalog.view.pool(), { symbol: 'NVDA', name: 'NVIDIA' }]);
    await run;
  });

  it('fails loudly when the job says failed, without selecting', async () => {
    const { service, jobs } = configureService();

    const run = service.ensure('NVDA', 'polygon_split_adjusted');
    await flush();
    jobs.emit('job-1', {
      type: 'job.failed',
      code: 'provider_422',
      message: 'The vendor refused the range.',
    });
    const ready = await run;

    expect(ready).toBe(false);
    expect(service.active()).toMatchObject({
      symbol: 'NVDA',
      phase: 'failed',
      reason: 'provider_422',
      message: 'The vendor refused the range.',
    });
  });

  it('does not accept a completed job whose bars never landed', async () => {
    const { service, jobs } = configureService();

    const run = service.ensure('NVDA', 'polygon_split_adjusted');
    await flush();
    jobs.emit('job-1', { type: 'job.completed' });
    const ready = await run;

    expect(ready).toBe(false);
    expect(service.active()).toMatchObject({
      symbol: 'NVDA',
      phase: 'failed',
      reason: 'backfill_empty',
    });
  });

  it('fails when backfill defaults are unreadable, without starting a job', async () => {
    const { service, jobs } = configureService({
      defaults: { kind: 'unavailable', message: 'The data plane did not answer.' },
    });

    const ready = await service.ensure('NVDA', 'polygon_split_adjusted');

    expect(ready).toBe(false);
    expect(jobs.started).toEqual([]);
    expect(service.active()).toMatchObject({
      phase: 'failed',
      reason: 'backfill_defaults_unavailable',
    });
  });

  it('cancels the job, disarms the stream, and clears the strip', async () => {
    const { service, jobs, catalog } = configureService();

    const run = service.ensure('NVDA', 'polygon_split_adjusted');
    await flush();
    await service.cancel();

    // A late frame from the cancelled job must not re-open the gate or
    // settle anything: the stream was closed and the run disarmed.
    jobs.emit('job-1', { type: 'job.completed' });
    const ready = await run;

    expect(jobs.cancelled).toEqual(['job-1']);
    expect(ready).toBe(false);
    expect(service.active()).toBeNull();
    expect(catalog.view.reloadCount).toBe(0);
  });

  it('supersedes an in-flight gate when another symbol is picked', async () => {
    const { service, jobs } = configureService();

    const first = service.ensure('NVDA', 'polygon_split_adjusted');
    await flush();
    const second = service.ensure('AMD', 'polygon_split_adjusted');
    await flush();

    expect(service.active()).toMatchObject({ symbol: 'AMD', phase: 'backfilling' });

    // The abandoned first job completing must not open the AMD gate.
    jobs.emit('job-1', { type: 'job.completed' });
    jobs.emit('job-2', { type: 'job.completed' });

    expect(await first).toBe(false);
    expect(await second).toBe(false);
    // AMD's gate is the failure the strip carries — not NVDA's.
    expect(service.active()).toMatchObject({ symbol: 'AMD', reason: 'backfill_empty' });
  });

  it('coalesces a repeated pick of the symbol already being ensured', async () => {
    const { service, jobs, catalog } = configureService();

    const first = service.ensure('NVDA', 'polygon_split_adjusted');
    await flush();
    const second = service.ensure('NVDA', 'polygon_split_adjusted');

    expect(jobs.started).toHaveLength(1);
    catalog.view.pool.set([...catalog.view.pool(), { symbol: 'NVDA', name: 'NVIDIA' }]);
    jobs.emit('job-1', { type: 'job.completed' });

    expect(await first).toBe(true);
    expect(await second).toBe(true);
  });

  it('cancels a job accepted after the operator already walked away', async () => {
    const { service, jobs, catalog } = configureService();

    // Hold startJob so the gate is stuck mid-submission — the exact window
    // where no job id exists yet and the old field-based state lost it.
    let release!: (id: string) => void;
    jobs.holdStart = new Promise<string>((resolve) => {
      release = resolve;
    });

    const run = service.ensure('NVDA', 'polygon_split_adjusted');
    await flush();
    const cancelling = service.cancel();
    await flush();
    release('job-1');
    await flush();
    await cancelling;

    // Submission then resolves into a disarmed gate: the just-accepted job
    // is cancelled, the run answers false, and nothing re-opens the strip.
    expect(jobs.cancelled).toEqual(['job-1']);
    expect(await run).toBe(false);
    expect(service.active()).toBeNull();
    expect(catalog.view.reloadCount).toBe(0);
  });
});

describe('fitBackfillWindow', () => {
  // One table row per weekday of a real week: the original bug was
  // weekday-dependent (Mon–Thu composed 1831–1833 inclusive days against
  // the 1830 cap; only a Saturday pass survived), so the fixture has to
  // walk a full week, not use whatever day CI happens to run on.
  const DAYS = ['2026-09-14', '2026-09-15', '2026-09-16', '2026-09-17', '2026-09-18', '2026-09-19', '2026-09-20'];

  it.each(DAYS)('fits an inclusive window under the cap from %s', (todayIso) => {
    const { start, end } = fitBackfillWindow(todayIso, DEFAULTS.max_trading_range_days);

    expect(tradingRangeSpanDays(start, end)).toBeLessThanOrEqual(
      DEFAULTS.max_trading_range_days,
    );
    expect(tradingRangeRejection(start, end, DEFAULTS.max_trading_range_days)).toBeNull();
  });

  it('ends at a trading day, never a weekend', () => {
    for (const todayIso of DAYS) {
      const { end } = fitBackfillWindow(todayIso, DEFAULTS.max_trading_range_days);
      const weekday = new Date(`${end}T00:00:00Z`).getUTCDay();
      expect(weekday).toBeGreaterThanOrEqual(1);
      expect(weekday).toBeLessThanOrEqual(5);
    }
  });
});
