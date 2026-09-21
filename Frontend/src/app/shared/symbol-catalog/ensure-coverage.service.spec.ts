import { TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach } from 'vitest';

import { JobsService, type JobStreamEvent } from '../../services/jobs.service';
import { DataLakeService } from '../data-lake';
import type { BackfillDefaults, DataLakeRead } from '../data-lake';
import { fakeTickerCatalog, provideFakeTickerCatalog } from '../ticker-catalog/testing/fake-ticker-catalog';
import type { FakeTickerCatalog } from '../ticker-catalog/testing/fake-ticker-catalog';
import type { TickerOption } from '../ticker-range-picker/ticker-range-picker.types';
import { EnsureCoverageService } from './ensure-coverage.service';

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
    emit(jobId, event) {
      handlers.get(jobId)?.(event);
    },
  };
  const catalog = fakeTickerCatalog([SPY]);
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      provideFakeTickerCatalog(catalog),
      {
        provide: JobsService,
        useValue: {
          startJob: async (type: string, payload: Record<string, unknown>) => {
            if (jobs.startError !== null) throw jobs.startError;
            started.push({ type, payload });
            return `job-${started.length}`;
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
});
