import { signal } from '@angular/core';
import { fireEvent, render, screen, within } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it, vi } from 'vitest';

import { JobsService, type JobStreamEvent } from '../../services/jobs.service';
import { DataLakeObservatoryComponent } from './data-lake-observatory.component';
import {
  ArtifactDetail,
  BackfillDefaults,
  CoverageDay,
  CoverageResponse,
  DataLakeRead,
  DataLakeService,
  RunReadinessResponse,
  StorageSummaryResponse,
  SymbolRunReadiness,
} from '../../shared/data-lake';
import { fakePickerWorld } from '../../shared/symbol-picker/testing/fake-picker-world';
import type { VendorSymbolEntry } from '../../shared/symbol-catalog/vendor-catalog.service';

/** 09:30 America/New_York on an EDT date, as int64 ms UTC. */
function sessionOpenMs(year: number, month: number, day: number): number {
  return Date.UTC(year, month - 1, day, 13, 30);
}

const DEFAULTS: BackfillDefaults = {
  market: 'usa',
  lean_image_digest: 'sha256:pinned',
  max_trading_range_days: 1830,
  max_symbol_length: 20,
  // 2021-10-05, calendar-anchored — the oldest day the provider plan serves.
  provider_history_start_ms: Date.UTC(2021, 9, 5, 12),
};

const EMPTY_STORAGE: StorageSummaryResponse = { market: 'usa', kinds: [], symbols: [] };

function readinessEntry(overrides: Partial<SymbolRunReadiness> & { symbol: string }): SymbolRunReadiness {
  return {
    first_trading_date_ms: sessionOpenMs(2026, 5, 18),
    last_trading_date_ms: sessionOpenMs(2026, 5, 19),
    session_count: 2,
    run_ready_count: 2,
    unrebuildable_count: 0,
    rebuild_first_trading_date_ms: null,
    rebuild_last_trading_date_ms: null,
    ...overrides,
  };
}

/** On disk since before the lake kept adjustment records: nothing of it is readable. */
const GLD = readinessEntry({
  symbol: 'GLD',
  run_ready_count: 0,
  rebuild_first_trading_date_ms: sessionOpenMs(2026, 5, 18),
  rebuild_last_trading_date_ms: sessionOpenMs(2026, 5, 19),
});
const SPY = readinessEntry({ symbol: 'SPY' });
/** Its oldest sessions predate the provider's history, so its rebuild window starts later than its span. */
const AMZN = readinessEntry({
  symbol: 'AMZN',
  first_trading_date_ms: sessionOpenMs(2021, 9, 22),
  last_trading_date_ms: sessionOpenMs(2026, 5, 19),
  session_count: 1254,
  run_ready_count: 0,
  unrebuildable_count: 9,
  rebuild_first_trading_date_ms: sessionOpenMs(2021, 10, 5),
  rebuild_last_trading_date_ms: sessionOpenMs(2026, 5, 19),
});

function readiness(symbols: readonly SymbolRunReadiness[]): DataLakeRead<RunReadinessResponse> {
  return {
    kind: 'ok',
    value: { market: 'usa', price_adjustment_mode: 'polygon_split_adjusted', data_type: 'trade', symbols },
  };
}

function coverage(symbol: string, days: readonly CoverageDay[]): DataLakeRead<CoverageResponse> {
  return {
    kind: 'ok',
    value: {
      market: 'usa',
      symbol,
      data_type: 'trade',
      resolution: 'minute',
      provider: 'polygon',
      price_adjustment_mode: 'polygon_split_adjusted',
      days,
    },
  };
}

const GLD_COVERAGE = coverage('GLD', [
  {
    trading_date_ms: sessionOpenMs(2026, 5, 18),
    status: 'needs_rebuild',
    artifact_id: 11,
    rebuild_reason: 'adjustment_record_missing',
  },
  {
    trading_date_ms: sessionOpenMs(2026, 5, 19),
    status: 'needs_rebuild',
    artifact_id: 12,
    rebuild_reason: 'adjustment_record_missing',
  },
]);

const ARTIFACT: ArtifactDetail = {
  id: 11,
  artifact_kind: 'minute_trade',
  market: 'usa',
  symbol: 'GLD',
  trading_date_ms: sessionOpenMs(2026, 5, 18),
  resolution: 'minute',
  data_type: 'trade',
  provider: 'polygon',
  provider_params: { adjusted: true },
  price_adjustment_mode: 'polygon_split_adjusted',
  data_contract_hash: 'dch-1234',
  content_hash: 'sha256-abcd',
  file_path: 'equity/usa/minute/gld/20260518_trade.zip',
  file_size_bytes: 1024,
  status: 'complete',
  row_count: 390,
  first_bar_start_ms: sessionOpenMs(2026, 5, 18),
  last_bar_start_ms: sessionOpenMs(2026, 5, 18),
  fetched_at_ms: sessionOpenMs(2026, 5, 18) + 1_000,
  completed_at_ms: sessionOpenMs(2026, 5, 18) + 2_000,
  attempt_count: 1,
  last_error: null,
  error_message: null,
};

function vendorEntry(symbol: string): VendorSymbolEntry {
  return { symbol, name: symbol, asset_class: 'us_equity', exchange: 'ARCA', status: 'active' };
}

const DARK: DataLakeRead<BackfillDefaults> = { kind: 'unavailable', message: 'The data plane did not respond.' };

async function renderObservatory(
  symbols: readonly SymbolRunReadiness[] = [GLD, SPY],
  defaults: DataLakeRead<BackfillDefaults> = { kind: 'ok', value: DEFAULTS },
) {
  const lake = {
    // A fresh answer per read, as the wire gives: nothing downstream may lean on object identity.
    runReadiness: vi.fn(async () => readiness(symbols)),
    storageSummary: vi.fn().mockResolvedValue({ kind: 'ok', value: EMPTY_STORAGE }),
    backfillDefaults: vi.fn().mockResolvedValue(defaults),
    coverage: vi.fn().mockResolvedValue(GLD_COVERAGE),
    artifact: vi.fn().mockResolvedValue({ kind: 'ok', value: ARTIFACT }),
  };
  // Each backfill is its own job; the stream handler is kept per job so a
  // test can end one run and watch whether the next begins.
  const handlers = new Map<string, (event: JobStreamEvent) => void>();
  let started = 0;
  const startJob = vi.fn(async () => `job-${++started}`);
  const onEvent = vi.fn((jobId: string, handler: (event: JobStreamEvent) => void) => {
    handlers.set(jobId, handler);
    return vi.fn();
  });
  const view = await render(DataLakeObservatoryComponent, {
    providers: [
      { provide: DataLakeService, useValue: lake },
      {
        provide: JobsService,
        useValue: {
          startJob,
          cancelJob: vi.fn(),
          onEvent,
          jobs: signal([]),
          historyTrimmed: signal(new Set<string>()),
        },
      },
      ...fakePickerWorld(
        [],
        symbols.map((entry) => vendorEntry(entry.symbol)),
      ).providers,
    ],
  });
  await screen.findByRole('button', { name: 'Show GLD sessions' });
  // Delivered as the stream delivers it, with no change detection forced
  // in between: what follows a frame must not depend on a render landing first.
  const endJob = (jobId: string, type: 'job.completed' | 'job.failed'): void => {
    handlers.get(jobId)?.({ type, code: 'io_error', message: 'disk full' });
  };
  return { view, lake, startJob, endJob };
}

function specOf(startJob: ReturnType<typeof vi.fn>, call: number): Record<string, unknown> {
  return (startJob.mock.calls[call] as [string, { spec: Record<string, unknown> }])[1].spec;
}

describe('DataLakeObservatoryComponent', () => {
  it('says up front that symbols on disk cannot be read, and that a backfill writes what is missing', async () => {
    const { lake } = await renderObservatory();

    expect(lake.runReadiness).toHaveBeenCalledWith('polygon_split_adjusted', 'trade');
    expect(
      screen.getByRole('heading', { name: '1 of 2 symbols needs a rebuild before a run can read it' }),
    ).toBeTruthy();
    expect(screen.getByText('A backfill fixes this.')).toBeTruthy();

    const gld = screen.getByRole('button', { name: 'Show GLD sessions' }).closest('tr') as HTMLElement;
    expect(within(gld).getByText('No · needs rebuild')).toBeTruthy();
    const spy = screen.getByRole('button', { name: 'Show SPY sessions' }).closest('tr') as HTMLElement;
    expect(within(spy).getByText('Yes')).toBeTruthy();
    expect(within(spy).queryByRole('button', { name: 'Rebuild SPY' })).toBeNull();
  });

  it('shows what a run finds for a picked session, and opens its receipt', async () => {
    const { lake } = await renderObservatory();

    fireEvent.click(await screen.findByRole('button', { name: 'GLD, 2026-05-18, Needs Rebuild' }));

    // The inspector's own check row, not the explainer card or the backfill panel's list.
    const record = (
      await screen.findByText('Adjustment record', { selector: '.checks__label' })
    ).closest('li') as HTMLElement;
    expect(within(record).getByText('Missing')).toBeTruthy();
    expect(screen.getByText("A run can't read this session")).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'View receipt' }));
    await vi.waitFor(() => expect(lake.artifact).toHaveBeenCalledWith(11));
    expect(await screen.findByText('dch-1234')).toBeTruthy();
  });

  it('rebuilds every symbol over its own window, one after another', async () => {
    const { lake, startJob, endJob } = await renderObservatory([GLD, AMZN]);

    fireEvent.click(screen.getByRole('button', { name: 'Rebuild all 2 symbols' }));
    await vi.waitFor(() => expect(startJob).toHaveBeenCalledTimes(1));

    // AMZN starts at the oldest session the provider still serves, not at its
    // first held one: a window reaching past that floor aborts the whole run.
    expect(specOf(startJob, 0)).toMatchObject({
      symbols: ['AMZN'],
      start_trading_date_ms: Date.UTC(2021, 9, 5, 12),
      end_trading_date_ms: Date.UTC(2026, 4, 19, 12),
      price_adjustment_mode: 'polygon_split_adjusted',
    });
    expect(screen.getByText(/Rebuilding AMZN \(1 of 2\)/)).toBeTruthy();

    endJob('job-1', 'job.completed');
    await vi.waitFor(() => expect(startJob).toHaveBeenCalledTimes(2));
    // The table and banner follow each finished symbol, not only the last.
    await vi.waitFor(() => expect(lake.runReadiness).toHaveBeenCalledTimes(2));
    expect(specOf(startJob, 1)).toMatchObject({
      symbols: ['GLD'],
      start_trading_date_ms: Date.UTC(2026, 4, 18, 12),
      end_trading_date_ms: Date.UTC(2026, 4, 19, 12),
    });
  });

  it('stops a rebuild of every symbol when one run fails, and re-reads the lake', async () => {
    const { lake, startJob, endJob } = await renderObservatory([GLD, AMZN]);

    fireEvent.click(screen.getByRole('button', { name: 'Rebuild all 2 symbols' }));
    await vi.waitFor(() => expect(startJob).toHaveBeenCalledTimes(1));
    endJob('job-1', 'job.failed');

    await vi.waitFor(() => expect(lake.runReadiness).toHaveBeenCalledTimes(2));
    await vi.waitFor(() => expect(screen.queryByText(/\(\d of 2\)/)).toBeNull());
    expect(startJob).toHaveBeenCalledTimes(1);
  });

  it('ends a rebuild of every symbol when the operator moves to another view', async () => {
    // The backfill panel writes the view on screen. Left running, the rest of
    // the list would be fetched into raw over windows composed for
    // split-adjusted, and the sessions it set out to rebuild would stay unreadable.
    const { startJob, endJob } = await renderObservatory([GLD, AMZN]);

    fireEvent.click(screen.getByRole('button', { name: 'Rebuild all 2 symbols' }));
    await vi.waitFor(() => expect(startJob).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole('button', { name: 'Raw' }));
    endJob('job-1', 'job.completed');

    await vi.waitFor(() => expect(screen.queryByText(/\(\d of 2\)/)).toBeNull());
    expect(startJob).toHaveBeenCalledTimes(1);
    expect(specOf(startJob, 0)).toMatchObject({ price_adjustment_mode: 'polygon_split_adjusted' });
  });

  it('starts nothing when the backfill form cannot run, and says why', async () => {
    const { startJob } = await renderObservatory([GLD, AMZN], DARK);

    fireEvent.click(screen.getByRole('button', { name: 'Rebuild all 2 symbols' }));

    expect(await screen.findByText('Backfill is unavailable until the data plane answers.')).toBeTruthy();
    await vi.waitFor(() => expect(screen.queryByText(/\(\d of 2\)/)).toBeNull());
    expect(startJob).not.toHaveBeenCalled();
  });

  it('reads and rebuilds the bars the operator picks', async () => {
    const { lake, startJob } = await renderObservatory();

    fireEvent.click(screen.getByRole('button', { name: 'Quote' }));
    await vi.waitFor(() =>
      expect(lake.runReadiness).toHaveBeenLastCalledWith('polygon_split_adjusted', 'quote'),
    );
    await vi.waitFor(() =>
      expect(lake.coverage).toHaveBeenLastCalledWith(expect.objectContaining({ dataType: 'quote' })),
    );

    // A backfill derives quote bars only alongside trade, so the rebuild asks for both.
    fireEvent.click(await screen.findByRole('button', { name: 'Rebuild GLD' }));
    await vi.waitFor(() => expect(startJob).toHaveBeenCalledTimes(1));
    expect(specOf(startJob, 0)).toMatchObject({ symbols: ['GLD'], data_types: ['trade', 'quote'] });
  });

  it('reads the view the operator switches to, and refuses to backfill one nothing derives', async () => {
    const { lake } = await renderObservatory();

    fireEvent.click(screen.getByRole('button', { name: 'Lean Adjusted' }));

    await vi.waitFor(() => expect(lake.runReadiness).toHaveBeenLastCalledWith('lean_adjusted', 'trade'));
    expect(await screen.findByText(/Nothing derives/)).toBeTruthy();
    expect((screen.getByRole('button', { name: 'Run backfill' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('keeps the grid and the picked session on screen while the lake is read again', async () => {
    // Every finished run re-reads readiness; that alone must not blank a grid
    // whose symbol, window and view have not changed.
    const { lake, startJob, endJob } = await renderObservatory();
    fireEvent.click(await screen.findByRole('button', { name: 'GLD, 2026-05-18, Needs Rebuild' }));
    await screen.findByText("A run can't read this session");
    // Every later coverage read stays pending, so a grid it blanked would stay blank.
    lake.coverage.mockImplementation(() => new Promise<never>(() => undefined));
    // The readiness re-read changes SPY, so the test can see it land.
    lake.runReadiness.mockImplementation(async () =>
      readiness([GLD, { ...SPY, session_count: 3, run_ready_count: 3 }]),
    );

    fireEvent.click(screen.getByRole('button', { name: 'Rebuild GLD only' }));
    await vi.waitFor(() => expect(startJob).toHaveBeenCalledTimes(1));
    endJob('job-1', 'job.completed');

    const spy = screen.getByRole('button', { name: 'Show SPY sessions' }).closest('tr') as HTMLElement;
    await vi.waitFor(() => expect(within(spy).getAllByText('3')).toHaveLength(2));
    expect(lake.coverage).toHaveBeenCalledTimes(2);
    expect(screen.getByRole('button', { name: 'GLD, 2026-05-18, Needs Rebuild' })).toBeTruthy();
    expect(screen.getByText("A run can't read this session")).toBeTruthy();
  });

  it('passes AXE with a symbol that needs a rebuild and a picked session', async () => {
    await renderObservatory();
    fireEvent.click(await screen.findByRole('button', { name: 'GLD, 2026-05-18, Needs Rebuild' }));
    await screen.findByText("A run can't read this session");

    const results = await axe.run(document.body, {
      rules: { 'color-contrast': { enabled: false }, region: { enabled: false } },
    });

    expect(results.violations).toEqual([]);
  });
});
