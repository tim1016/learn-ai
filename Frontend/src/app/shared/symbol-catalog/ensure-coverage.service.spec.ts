import { TestBed } from '@angular/core/testing';
import { describe, it, expect } from 'vitest';

import type { SymbolCoverageSpan } from '../data-lake';
import { JobsService, type JobStreamEvent } from '../../services/jobs.service';
import { DataLakeService, tradingRangeRejection, tradingRangeSpanDays } from '../data-lake';
import type { BackfillDefaults, DataLakeRead } from '../data-lake';
import {
  EnsureCoverageService,
  fitBackfillWindow,
  type CoverageGateSession,
} from './ensure-coverage.service';
import { etIsoDate } from '../date/et-midnight';
import {
  fakeTickerCatalog,
  provideFakeTickerCatalog,
} from '../ticker-catalog/testing/fake-ticker-catalog';
import type { FakeTickerCatalog } from '../ticker-catalog/testing/fake-ticker-catalog';
import type { TickerOption } from '../ticker-range-picker/ticker-range-picker.types';

const DEFAULTS: BackfillDefaults = {
  market: 'usa',
  lean_image_digest: 'digest@sha256:deadbeef',
  max_trading_range_days: 1830,
  max_symbol_length: 20,
  // 2021-09-23, calendar-anchored — five years before SEP_2026's day plus
  // the margin over the provider's exclusive boundary.
  provider_history_start_ms: 1632398400000,
};

/** 2024-08-06 and 2026-09-04, both 09:30 ET — the anchor a trading date carries. */
const AUG_2024 = 1722951000000;
const SEP_2026 = 1788528600000;

const SPY: TickerOption = {
  symbol: 'SPY',
  name: 'SPDR S&P 500 ETF',
  exchange: 'ARCA',
  firstHeld: '2024-05-20',
  lastHeld: '2026-09-04',
};

/** When set, every fresh lake read fails with this message. */
let storageFailure: string | null = null;

interface FakeJobs {
  readonly started: { type: string; payload: Record<string, unknown> }[];
  readonly cancelled: string[];
  startError: Error | null;
  /** When set, `startJob` awaits this instead of resolving immediately. */
  holdStart: Promise<string> | null;
  emit(jobId: string, event: JobStreamEvent): void;
}

function configureService(options: { defaults?: DataLakeRead<BackfillDefaults> } = {}): {
  service: EnsureCoverageService;
  jobs: FakeJobs;
  catalog: FakeTickerCatalog;
  /** The fresh lake read the gate's verdict comes from. Tests mutate it. */
  freshCoverage: { symbols: SymbolCoverageSpan[] };
} {
  const started: FakeJobs['started'] = [];
  const cancelled: string[] = [];
  // The runner registers its fold through onEvent after startJob resolves,
  // so emitting goes through whichever handler is registered for the job id.
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
  // When non-null, the fresh lake read fails with this message — the lake
  // going dark between job submission and completion.
  storageFailure = null;
  const catalog = fakeTickerCatalog([SPY]);
  // The lake's cached pool still answers with only SPY — what a fresh read
  // returns is this knob's business, not the pool's.
  const freshCoverage: { symbols: SymbolCoverageSpan[] } = {
    symbols: [
      {
        symbol: 'SPY',
        first_trading_date_ms: AUG_2024,
        last_trading_date_ms: SEP_2026,
        artifact_count: 2290,
      },
    ],
  };
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
          storageSummary: async () =>
            storageFailure !== null
              ? { kind: 'unavailable' as const, message: storageFailure }
              : {
                  kind: 'ok' as const,
                  value: {
                    market: 'usa' as const,
                    kinds: [],
                    symbols: freshCoverage.symbols,
                  },
                },
        },
      },
    ],
  });
  return {
    service: TestBed.inject(EnsureCoverageService),
    jobs,
    catalog,
    freshCoverage,
  };
}

async function flush(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0));
}

/** One typed failure as the worker puts it on the wire; overrides as needed. */
function failure(
  overrides: Partial<Record<string, unknown>> & { reason: string },
): Record<string, unknown> {
  return {
    artifact_kind: 'time_series_bars',
    symbol: 'NVDA',
    data_type: 'trade',
    detail: null,
    provider_status_code: null,
    attempt_count: 1,
    trading_date_ms: SEP_2026,
    ...overrides,
  };
}

/** One `data_lake.backfill_day` frame for `dayIndex`, carrying `failures`. */
function dayFrame(dayIndex: number, failures: Record<string, unknown>[]): JobStreamEvent {
  return {
    type: 'data_lake.backfill_day',
    trading_date_ms: SEP_2026,
    day_index: dayIndex,
    total_days: 3,
    days_remaining: 3 - dayIndex,
    fetched_count: 0,
    reused_count: 0,
    failures,
  };
}

function markCovered(freshCoverage: { symbols: SymbolCoverageSpan[] }): void {
  freshCoverage.symbols = [
    ...freshCoverage.symbols,
    {
      symbol: 'NVDA',
      first_trading_date_ms: AUG_2024,
      last_trading_date_ms: SEP_2026,
      artifact_count: 4,
    },
  ];
}

describe('EnsureCoverageService', () => {
  it('answers a held symbol immediately, without starting a job', async () => {
    const { service, jobs } = configureService();

    const session = service.ensure('SPY', 'polygon_split_adjusted');

    await expect(session.done).resolves.toBe(true);
    expect(session.state()).toBeNull();
    expect(jobs.started).toEqual([]);
  });

  it('backfills an unheld symbol with a full-history trade-bar spec', async () => {
    const { service, jobs, catalog, freshCoverage } = configureService();

    const session = service.ensure('NVDA', 'polygon_split_adjusted');
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
    expect(tradingRangeRejection(startIso, endIso, DEFAULTS.max_trading_range_days)).toBeNull();
    // In flight: the gate names the symbol and its tree, and no failure.
    expect(session.state()).toMatchObject({
      symbol: 'NVDA',
      mode: 'polygon_split_adjusted',
      phase: 'backfilling',
    });

    // The job completes and a fresh lake read now holds the symbol.
    markCovered(freshCoverage);
    jobs.emit('job-1', { type: 'job.completed' });
    const ready = await session.done;

    expect(ready).toBe(true);
    expect(session.state()).toBeNull();
    // Completion re-read the catalog so every picker sees the new coverage.
    expect(catalog.view.reloadCount).toBeGreaterThan(0);
  });

  it('shows progress ticks while the backfill runs', async () => {
    const { service, jobs, freshCoverage } = configureService();

    const session = service.ensure('NVDA', 'polygon_split_adjusted');
    await flush();
    jobs.emit('job-1', { type: 'job.progress', current: 250, total: 1000 });
    await flush();

    expect(session.state()).toMatchObject({
      phase: 'backfilling',
      percent: 25,
    });

    markCovered(freshCoverage);
    jobs.emit('job-1', { type: 'job.completed' });
    await session.done;
  });

  it('fails loudly when the job says failed, without selecting', async () => {
    const { service, jobs } = configureService();

    const session = service.ensure('NVDA', 'polygon_split_adjusted');
    await flush();
    jobs.emit('job-1', {
      type: 'job.failed',
      code: 'provider_422',
      message: 'The vendor refused the range.',
    });
    const ready = await session.done;

    expect(ready).toBe(false);
    // The failure stays on the session's strip until retry or dismissal.
    expect(session.state()).toMatchObject({
      symbol: 'NVDA',
      mode: 'polygon_split_adjusted',
      phase: 'failed',
      reason: 'provider_422',
      message: 'The vendor refused the range.',
    });
  });

  it('does not accept a completed job whose bars never landed', async () => {
    const { service, jobs } = configureService();

    const session = service.ensure('NVDA', 'polygon_split_adjusted');
    await flush();
    jobs.emit('job-1', { type: 'job.completed' });
    const ready = await session.done;

    expect(ready).toBe(false);
    expect(session.state()).toMatchObject({
      symbol: 'NVDA',
      phase: 'failed',
      reason: 'backfill_empty',
    });
  });

  // The job framework reports a run the provider refused as `job.completed`
  // — the job ran; the backfill did not — so a gate watching only the
  // lifecycle could say nothing beyond "empty", and an operator could not
  // tell a plan limit from a symbol with no bars.
  it('reports the run’s own reason when a refused run wrote nothing', async () => {
    const { service, jobs } = configureService();

    const session = service.ensure('NVDA', 'polygon_split_adjusted');
    await flush();
    jobs.emit('job-1', {
      type: 'data_lake.backfill_day',
      trading_date_ms: 1632398400000,
      day_index: 1,
      total_days: 1255,
      days_remaining: 1254,
      fetched_count: 0,
      reused_count: 3,
      failures: [
        {
          artifact_kind: 'time_series_bars',
          symbol: 'NVDA',
          data_type: 'trade',
          reason: 'provider_entitlement_error',
          detail: "Polygon 403 for NVDA: your plan doesn't include this data timeframe.",
          provider_status_code: null,
          attempt_count: 1,
          trading_date_ms: 1632398400000,
        },
        {
          artifact_kind: 'time_series_bars',
          symbol: null,
          data_type: null,
          reason: 'run_aborted',
          detail: 'stopped after a globally-fatal provider_entitlement_error',
          provider_status_code: null,
          attempt_count: 0,
          trading_date_ms: null,
        },
      ],
    });
    jobs.emit('job-1', { type: 'job.completed' });
    const ready = await session.done;

    expect(ready).toBe(false);
    expect(session.state()).toMatchObject({
      symbol: 'NVDA',
      phase: 'failed',
      reason: 'provider_entitlement_error',
      message: "Polygon 403 for NVDA: your plan doesn't include this data timeframe.",
    });
  });

  // The worker walks oldest session first and breaks right after the frame
  // whose failure was fatal, so the *last* failing frame is the cause. An
  // earlier day can fail benignly — a session before the symbol listed —
  // and reporting that one hands the operator the wrong reason entirely.
  it('reports the failure that ended the run, not the first one seen', async () => {
    const { service, jobs } = configureService();

    const session = service.ensure('NVDA', 'polygon_split_adjusted');
    await flush();
    jobs.emit('job-1', dayFrame(1, [failure({ reason: 'provider_no_data', detail: 'No bars for this session.' })]));
    jobs.emit('job-1', dayFrame(2, [failure({ reason: 'provider_no_data', detail: 'No bars for this session.' })]));
    jobs.emit(
      'job-1',
      dayFrame(3, [
        failure({ reason: 'provider_rate_limited', detail: 'Polygon 429 for NVDA.' }),
        failure({ reason: 'run_aborted', artifact_kind: 'time_series_bars', detail: 'stopped early' }),
      ]),
    );
    jobs.emit('job-1', { type: 'job.completed' });

    expect(await session.done).toBe(false);
    expect(session.state()).toMatchObject({
      reason: 'provider_rate_limited',
      message: 'Polygon 429 for NVDA.',
    });
  });

  // A Phase 0 metadata failure is day-independent and does not stop Pass 1,
  // so it says nothing about whether the day's bars landed — the data plane
  // draws exactly this distinction in `_is_bar_affecting_failure` (#1900).
  // It is also appended *before* the bar failure, so position alone loses.
  it('prefers the bar failure over a day-independent metadata failure', async () => {
    const { service, jobs } = configureService();

    const session = service.ensure('NVDA', 'polygon_split_adjusted');
    await flush();
    jobs.emit(
      'job-1',
      dayFrame(1, [
        failure({
          reason: 'launcher_unreachable',
          artifact_kind: 'metadata',
          symbol: null,
          detail: 'The LEAN launcher did not answer.',
        }),
        failure({ reason: 'provider_entitlement_error', detail: 'Polygon 403 for NVDA.' }),
      ]),
    );
    jobs.emit('job-1', { type: 'job.completed' });

    expect(await session.done).toBe(false);
    expect(session.state()).toMatchObject({
      reason: 'provider_entitlement_error',
      message: 'Polygon 403 for NVDA.',
    });
  });

  // A data plane too old to publish the floor must refuse loudly. Composing
  // against a missing field threw a RangeError out of the run, which nothing
  // caught — leaving the strip spinning on a gate that could never settle.
  it('refuses loudly when the data plane reports no provider history floor', async () => {
    const { service, jobs } = configureService({
      defaults: {
        kind: 'ok',
        value: { ...DEFAULTS, provider_history_start_ms: undefined as unknown as number },
      },
    });

    const session = service.ensure('NVDA', 'polygon_split_adjusted');

    expect(await session.done).toBe(false);
    expect(session.state()).toMatchObject({
      phase: 'failed',
      reason: 'backfill_history_floor_missing',
    });
    expect(jobs.started).toEqual([]);
  });

  // A run that failed some days but landed others is still a good pick: the
  // lake, not the failure list, decides whether the symbol became runnable.
  it('accepts a pick whose bars landed despite a reported failure', async () => {
    const { service, jobs, freshCoverage } = configureService();

    const session = service.ensure('NVDA', 'polygon_split_adjusted');
    await flush();
    jobs.emit('job-1', {
      type: 'data_lake.backfill_day',
      trading_date_ms: 1632398400000,
      day_index: 1,
      total_days: 2,
      days_remaining: 1,
      fetched_count: 0,
      reused_count: 0,
      failures: [
        {
          artifact_kind: 'time_series_bars',
          symbol: 'NVDA',
          data_type: 'trade',
          reason: 'provider_no_data',
          detail: 'No bars for this session.',
          provider_status_code: null,
          attempt_count: 1,
          trading_date_ms: 1632398400000,
        },
      ],
    });
    markCovered(freshCoverage);
    jobs.emit('job-1', { type: 'job.completed' });

    expect(await session.done).toBe(true);
    expect(session.state()).toBeNull();
  });

  it('fails when backfill defaults are unreadable, without starting a job', async () => {
    const { service, jobs } = configureService({
      defaults: {
        kind: 'unavailable',
        message: 'The data plane did not answer.',
      },
    });

    const session = service.ensure('NVDA', 'polygon_split_adjusted');

    await expect(session.done).resolves.toBe(false);
    expect(jobs.started).toEqual([]);
    expect(session.state()).toMatchObject({
      phase: 'failed',
      reason: 'backfill_defaults_unavailable',
    });
  });

  it('cancels the job, disarms the stream, and clears the strip', async () => {
    const { service, jobs, catalog } = configureService();

    const session = service.ensure('NVDA', 'polygon_split_adjusted');
    await flush();
    await session.cancel();

    // A late frame from the cancelled job must not re-open the gate or
    // settle anything: the stream was closed and the run disarmed.
    jobs.emit('job-1', { type: 'job.completed' });
    const ready = await session.done;

    expect(jobs.cancelled).toEqual(['job-1']);
    expect(ready).toBe(false);
    expect(session.state()).toBeNull();
    expect(catalog.view.reloadCount).toBe(0);
  });

  it('keeps unrelated symbol gates independent', async () => {
    const { service, jobs, freshCoverage } = configureService();

    const first = service.ensure('NVDA', 'polygon_split_adjusted');
    await flush();
    const second = service.ensure('AMD', 'polygon_split_adjusted');
    await flush();

    expect(first.state()).toMatchObject({
      symbol: 'NVDA',
      phase: 'backfilling',
    });
    expect(second.state()).toMatchObject({
      symbol: 'AMD',
      phase: 'backfilling',
    });
    expect(jobs.started).toHaveLength(2);

    markCovered(freshCoverage);
    jobs.emit('job-1', { type: 'job.completed' });
    expect(await first.done).toBe(true);
    expect(second.state()).toMatchObject({
      symbol: 'AMD',
      phase: 'backfilling',
    });

    jobs.emit('job-2', { type: 'job.completed' });
    expect(await second.done).toBe(false);
    expect(second.state()).toMatchObject({
      symbol: 'AMD',
      reason: 'backfill_empty',
    });
  });

  it('coalesces a repeated pick of the symbol already being ensured', async () => {
    const { service, jobs, freshCoverage } = configureService();

    const first = service.ensure('NVDA', 'polygon_split_adjusted');
    await flush();
    const second = service.ensure('NVDA', 'polygon_split_adjusted');

    expect(jobs.started).toHaveLength(1);
    markCovered(freshCoverage);
    jobs.emit('job-1', { type: 'job.completed' });

    expect(await first.done).toBe(true);
    expect(await second.done).toBe(true);
  });

  it('releases a coalesced session without killing its co-waiter', async () => {
    const { service, jobs, freshCoverage } = configureService();

    const first = service.ensure('NVDA', 'polygon_split_adjusted');
    await flush();
    const second = service.ensure('NVDA', 'polygon_split_adjusted');
    await flush();

    // Two cards legitimately wait on one run: the first card letting go
    // detaches its own strip but must not cancel the job the second is
    // still awaiting.
    await first.cancel();
    expect(jobs.cancelled).toEqual([]);
    expect(first.state()).toBeNull();
    expect(second.state()).toMatchObject({
      symbol: 'NVDA',
      phase: 'backfilling',
    });

    markCovered(freshCoverage);
    jobs.emit('job-1', { type: 'job.completed' });

    expect(await first.done).toBe(false);
    expect(await second.done).toBe(true);
  });

  // The reviewer's masked regression: judging membership against the cached
  // pool (which a test can edit) hides the fact that `reload()` is async —
  // the verdict must come from the fresh read, never the pool.
  it('judges coverage by a fresh read, not the catalog pool', async () => {
    const { service, jobs, catalog } = configureService();

    const session = service.ensure('NVDA', 'polygon_split_adjusted');
    await flush();
    // The pool was hand-updated, but the fresh read says otherwise.
    catalog.view.pool.set([
      ...catalog.view.pool(),
      { symbol: 'NVDA', name: 'NVIDIA', lastHeld: '2026-09-04' },
    ]);
    jobs.emit('job-1', { type: 'job.completed' });
    const ready = await session.done;

    expect(ready).toBe(false);
    expect(session.state()).toMatchObject({
      phase: 'failed',
      reason: 'backfill_empty',
    });
  });

  it('reports coverage_unknown when the fresh read fails', async () => {
    const { service, jobs, freshCoverage } = configureService();

    const session = service.ensure('NVDA', 'polygon_split_adjusted');
    await flush();
    // The lake goes dark between submission and completion; even the pool
    // being updated cannot make the verdict when no read answers.
    markCovered(freshCoverage);
    storageFailure = 'The data plane did not respond.';
    jobs.emit('job-1', { type: 'job.completed' });
    const ready = await session.done;

    expect(ready).toBe(false);
    expect(session.state()).toMatchObject({
      phase: 'failed',
      reason: 'coverage_unknown',
      message: 'The data plane did not respond.',
    });
  });

  it('keys the in-flight gate by symbol and tree, not symbol alone', async () => {
    const { service, jobs, freshCoverage } = configureService();

    const raw = service.ensure('NVDA', 'raw');
    await flush();
    const adjusted = service.ensure('NVDA', 'polygon_split_adjusted');
    await flush();

    // A raw gate says nothing about the split-adjusted tree: the second
    // pick must start its own backfill, not inherit the first's promise.
    expect(jobs.started).toHaveLength(2);
    expect(jobs.started[1].payload['spec']).toMatchObject({
      symbols: ['NVDA'],
      price_adjustment_mode: 'polygon_split_adjusted',
    });

    markCovered(freshCoverage);
    jobs.emit('job-1', { type: 'job.completed' });
    jobs.emit('job-2', { type: 'job.completed' });

    // Different trees are independent gates; neither may detach the other.
    expect(await raw.done).toBe(true);
    expect(await adjusted.done).toBe(true);
  });

  it('cancels a job accepted after the operator already walked away', async () => {
    const { service, jobs, catalog } = configureService();

    // Hold startJob so the gate is stuck mid-submission — the exact window
    // where no job id exists yet and the old field-based state lost it.
    let release!: (id: string) => void;
    jobs.holdStart = new Promise<string>((resolve) => {
      release = resolve;
    });

    const session: CoverageGateSession = service.ensure('NVDA', 'polygon_split_adjusted');
    await flush();
    const cancelling = session.cancel();
    await flush();
    release('job-1');
    await flush();
    await cancelling;

    // Submission then resolves into a disarmed gate: the just-accepted job
    // is cancelled, the run answers false, and nothing re-opens the strip.
    expect(jobs.cancelled).toEqual(['job-1']);
    expect(await session.done).toBe(false);
    expect(session.state()).toBeNull();
    expect(catalog.view.reloadCount).toBe(0);
  });

  it('renders a refusal without touching any running gate', async () => {
    const { service, jobs } = configureService();

    const running = service.ensure('AMD', 'raw');
    await flush();
    const refusal = service.refuse(
      'NVDA',
      'lean_adjusted',
      'view_not_backfillable',
      'Nothing derives the Lean Adjusted view.',
    );

    expect(jobs.started).toHaveLength(1);
    await expect(refusal.done).resolves.toBe(false);
    expect(refusal.state()).toMatchObject({
      symbol: 'NVDA',
      mode: 'lean_adjusted',
      phase: 'failed',
      reason: 'view_not_backfillable',
    });
    // The unrelated in-flight gate is nobody's refusal to cancel.
    await refusal.cancel();
    expect(refusal.state()).toBeNull();
    expect(running.state()).toMatchObject({
      symbol: 'AMD',
      phase: 'backfilling',
    });
    expect(jobs.cancelled).toEqual([]);
  });
});

describe('fitBackfillWindow', () => {
  // One table row per weekday of a real week: the original bug was
  // weekday-dependent (Mon–Thu composed 1831–1833 inclusive days against
  // the 1830 cap; only a Saturday pass survived), so the fixture has to
  // walk a full week, not use whatever day CI happens to run on.
  const DAYS = [
    '2026-09-14',
    '2026-09-15',
    '2026-09-16',
    '2026-09-17',
    '2026-09-18',
    '2026-09-19',
    '2026-09-20',
  ];

  /**
   * The floor the data plane reports for each of those days: five years
   * back plus the margin over Polygon's exclusive boundary, exactly as
   * `polygon_history_floor` computes it.
   */
  const floorFor = (todayIso: string): string => {
    const [year, month, day] = todayIso.split('-').map(Number);
    const anniversary = new Date(Date.UTC(year - 5, month - 1, day + 2));
    return anniversary.toISOString().slice(0, 10);
  };

  it.each(DAYS)('fits an inclusive window under the cap from %s', (todayIso) => {
    const { start, end } = fitBackfillWindow(
      todayIso,
      DEFAULTS.max_trading_range_days,
      floorFor(todayIso),
    );

    expect(tradingRangeSpanDays(start, end)).toBeLessThanOrEqual(DEFAULTS.max_trading_range_days);
    expect(tradingRangeRejection(start, end, DEFAULTS.max_trading_range_days)).toBeNull();
  });

  // The regression: clearing the cap is not clearing the provider. `cap - 4`
  // is exactly five years, and five years to the day is the one day a
  // five-year plan refuses — so every unheld pick's oldest day drew a 403
  // and the globally-fatal abort killed the run before a bar was written.
  it.each(DAYS)('never starts before the provider history floor from %s', (todayIso) => {
    const floor = floorFor(todayIso);

    const { start } = fitBackfillWindow(todayIso, DEFAULTS.max_trading_range_days, floor);

    expect(start >= floor).toBe(true);
  });

  // The field report, to the day: on 2026-09-21 the cap-derived start was
  // 2021-09-21, which Polygon answered 403 NOT_AUTHORIZED while serving the
  // day after it.
  it('clears the day the provider refused in the reported failure', () => {
    const { start } = fitBackfillWindow('2026-09-21', 1830, '2021-09-23');

    expect(start).toBe('2021-09-23');
    expect(start > '2021-09-21').toBe(true);
  });

  it('starts on a trading day even when the floor lands on a weekend', () => {
    // 2021-09-25 is a Saturday: walking it back to Friday would step
    // straight back outside the floor it exists to respect.
    const { start } = fitBackfillWindow(
      '2026-09-21',
      DEFAULTS.max_trading_range_days,
      '2021-09-25',
    );

    expect(start).toBe('2021-09-27');
  });

  it('keeps the cap-derived start when the floor is older than the cap allows', () => {
    // A floor this wide cannot bind, so the cap decides — and the resulting
    // window must be the widest the cap permits, not merely "not rejected".
    const { start, end } = fitBackfillWindow(
      '2026-09-21',
      DEFAULTS.max_trading_range_days,
      '2000-01-03',
    );

    // 2026-09-21 is a Monday, so `end - 1826` lands on Tuesday 2021-09-21
    // with no walk-back: the widest start the cap permits, 1827 inclusive
    // days. This is also the exact day Polygon refuses — which is why a
    // real floor must always win over it.
    expect(start).toBe('2021-09-21');
    expect(tradingRangeSpanDays(start, end)).toBe(1827);
    expect(tradingRangeRejection(start, end, DEFAULTS.max_trading_range_days)).toBeNull();
  });

  // The start is placed four days inside the cap precisely so no corrective
  // loop is needed: the weekday walk-back moves it at most two days earlier,
  // so the span cannot reach the cap at any cap value. If this ever fails,
  // the fix is to re-derive the offset — not to reinstate a loop.
  it('cannot reach the cap at any cap value, so no corrective step is owed', () => {
    for (const cap of [1830, 365, 90, 30, 11, 10, 7, 6, 5]) {
      for (let day = 0; day < 28; day++) {
        const todayIso = new Date(Date.UTC(2026, 0, 1 + day)).toISOString().slice(0, 10);
        const { start, end } = fitBackfillWindow(todayIso, cap, '2000-01-03');
        expect(tradingRangeSpanDays(start, end)).toBeLessThan(cap);
      }
    }
  });

  it('ends at a trading day, never a weekend', () => {
    for (const todayIso of DAYS) {
      const { end } = fitBackfillWindow(
        todayIso,
        DEFAULTS.max_trading_range_days,
        floorFor(todayIso),
      );
      const weekday = new Date(`${end}T00:00:00Z`).getUTCDay();
      expect(weekday).toBeGreaterThanOrEqual(1);
      expect(weekday).toBeLessThanOrEqual(5);
    }
  });
});
