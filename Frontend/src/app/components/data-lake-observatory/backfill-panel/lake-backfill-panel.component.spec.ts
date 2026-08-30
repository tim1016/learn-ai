import { signal } from '@angular/core';
import { fireEvent, render, screen, within } from '@testing-library/angular';
import axe from 'axe-core';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { JobsService, type JobState } from '../../../services/jobs.service';
import { DataLakeBackfillStore } from '../lib/data-lake-backfill.store';
import type { BackfillDefaults, BackfillFailure, PriceAdjustmentMode } from '../lib/data-lake.types';
import { LakeBackfillPanelComponent } from './lake-backfill-panel.component';

/** 09:30 America/New_York on 2026-05-20, as int64 ms UTC. */
const MAY_20_OPEN_MS = Date.UTC(2026, 4, 20, 13, 30);

const DEFAULTS: BackfillDefaults = {
  market: 'usa',
  lean_image_digest: 'sha256:pinned',
  max_trading_range_days: 1830,
  max_symbol_length: 20,
};

function fakeFailure(symbol: string): BackfillFailure {
  return {
    artifact_kind: 'minute_trade',
    symbol,
    trading_date_ms: MAY_20_OPEN_MS,
    data_type: 'trade',
    reason: 'provider_rate_limited',
    detail: null,
    provider_status_code: 429,
    attempt_count: 1,
  };
}

let originalEventSource: typeof EventSource;

function installEventSourceStub(): void {
  originalEventSource = globalThis.EventSource;
  class StubEventSource {
    onmessage: ((event: MessageEvent<string>) => void) | null = null;
    onerror: (() => void) | null = null;
    close(): void {}
  }
  (globalThis as unknown as { EventSource: typeof EventSource }).EventSource =
    StubEventSource as unknown as typeof EventSource;
}

interface PanelOptions {
  defaults?: BackfillDefaults | null;
  priceAdjustmentMode?: PriceAdjustmentMode;
  seedStartTradingDate?: string;
  seedEndTradingDate?: string;
  /** What `JobsService.jobs()` already holds when the panel mounts. */
  liveJobs?: readonly Partial<JobState>[];
}

async function renderPanel(options: PanelOptions = {}) {
  const startJob = vi.fn().mockResolvedValue('job-77');
  const cancelJob = vi.fn().mockResolvedValue(undefined);
  // DataLakeBackfillStore rides JobsService.onEvent() (#1856) instead of
  // opening its own EventSource — start() registers a listener through it.
  const onEvent = vi.fn().mockReturnValue(vi.fn());
  const jobs = signal(options.liveJobs ?? []);
  const view = await render(LakeBackfillPanelComponent, {
    providers: [{ provide: JobsService, useValue: { startJob, cancelJob, onEvent, jobs } }],
    componentInputs: {
      defaults: options.defaults === undefined ? DEFAULTS : options.defaults,
      seedSymbols: 'SPY',
      seedStartTradingDate: options.seedStartTradingDate ?? '2026-05-18',
      seedEndTradingDate: options.seedEndTradingDate ?? '2026-05-22',
      priceAdjustmentMode: options.priceAdjustmentMode ?? 'raw',
    },
  });
  const store = view.fixture.debugElement.injector.get(DataLakeBackfillStore);
  return { ...view, startJob, cancelJob, store };
}

describe('LakeBackfillPanelComponent', () => {
  beforeEach(() => installEventSourceStub());

  afterEach(() => {
    (globalThis as unknown as { EventSource: typeof EventSource }).EventSource =
      originalEventSource;
  });

  it('seeds the form from the window the page is showing', async () => {
    await renderPanel();

    expect((screen.getByLabelText('Symbols to backfill') as HTMLInputElement).value).toBe('SPY');
    expect((screen.getByLabelText('Backfill start date') as HTMLInputElement).value).toBe(
      '2026-05-18',
    );
  });

  it('submits a spec the backfill job accepts', async () => {
    const { startJob } = await renderPanel();

    fireEvent.click(screen.getByRole('button', { name: 'Run backfill' }));
    await vi.waitFor(() => expect(startJob).toHaveBeenCalled());

    const [type, payload] = startJob.mock.calls[0] as [string, { spec: Record<string, unknown> }];
    expect(type).toBe('data_lake_backfill');
    expect(payload.spec).toMatchObject({
      run_type: 'python_lab',
      market: 'usa',
      symbols: ['SPY'],
      start_trading_date: '2026-05-18',
      end_trading_date: '2026-05-22',
      data_types: ['trade'],
      lean_image_digest: 'sha256:pinned',
    });
  });

  it('adds quote bars alongside trade when asked, never on their own', async () => {
    const { startJob } = await renderPanel();

    fireEvent.click(screen.getByLabelText('Also derive quote bars'));
    fireEvent.click(screen.getByRole('button', { name: 'Run backfill' }));
    await vi.waitFor(() => expect(startJob).toHaveBeenCalled());

    const [, payload] = startJob.mock.calls[0] as [string, { spec: { data_types: string[] } }];
    expect(payload.spec.data_types).toEqual(['trade', 'quote']);
  });

  it('renders live progress and each session as it lands', async () => {
    const { store, detectChanges } = await renderPanel();

    fireEvent.click(screen.getByRole('button', { name: 'Run backfill' }));
    await vi.waitFor(() => expect(store.jobId()).toBe('job-77'));

    store.ingestEvent({ type: 'job.progress', current: 1, total: 3, unit: 'days' });
    store.ingestEvent({
      type: 'data_lake.backfill_day',
      trading_date_ms: MAY_20_OPEN_MS,
      day_index: 1,
      total_days: 3,
      days_remaining: 2,
      fetched_count: 1,
      reused_count: 0,
      failures: [],
    });
    detectChanges();

    expect(screen.getByText(/1 \/ 3 days/)).toBeTruthy();
    expect(screen.getByText('2026-05-20')).toBeTruthy();
    expect(screen.getByRole('progressbar').getAttribute('aria-valuenow')).toBe('33');
  });

  it('shows a typed failure reason through the receipt-label pipe', async () => {
    const { store, detectChanges } = await renderPanel();

    fireEvent.click(screen.getByRole('button', { name: 'Run backfill' }));
    await vi.waitFor(() => expect(store.jobId()).toBe('job-77'));

    store.ingestEvent({
      type: 'data_lake.backfill_day',
      trading_date_ms: MAY_20_OPEN_MS,
      day_index: 1,
      total_days: 1,
      days_remaining: 0,
      fetched_count: 0,
      reused_count: 0,
      failures: [
        {
          artifact_kind: 'minute_trade',
          symbol: 'SPY',
          trading_date_ms: MAY_20_OPEN_MS,
          data_type: 'trade',
          reason: 'provider_entitlement_error',
          detail: 'plan does not include this feed',
          provider_status_code: 403,
          attempt_count: 1,
        },
      ],
    });
    store.ingestEvent({ type: 'job.completed' });
    detectChanges();

    expect(screen.getByText('Provider Entitlement Error')).toBeTruthy();
    expect(screen.getByText('plan does not include this feed')).toBeTruthy();
  });

  it.each(['job.completed', 'job.failed', 'job.cancelled'])(
    'tells the page to re-read coverage after %s',
    async (terminalEvent) => {
      // `run_backfill` writes session by session, so a run that died — or
      // that the operator stopped — between per-day writes still left
      // completed artifacts on disk. Every terminal phase leaves the
      // heatmap stale, not just the successful one.
      const { store, fixture, detectChanges } = await renderPanel();
      const finished = vi.fn();
      fixture.componentInstance.runFinished.subscribe(finished);

      fireEvent.click(screen.getByRole('button', { name: 'Run backfill' }));
      await vi.waitFor(() => expect(store.jobId()).toBe('job-77'));
      store.ingestEvent({ type: terminalEvent, code: 'io_error', message: 'disk full' });
      detectChanges();

      expect(finished).toHaveBeenCalledTimes(1);
    },
  );

  it('refuses to submit without a pinned LEAN image digest', async () => {
    const { startJob } = await renderPanel({
      defaults: { ...DEFAULTS, lean_image_digest: null },
    });

    expect(
      screen.getByText(
        'The data plane has no pinned LEAN image digest, so a backfill spec cannot be composed.',
      ),
    ).toBeTruthy();
    expect((screen.getByRole('button', { name: 'Run backfill' }) as HTMLButtonElement).disabled).toBe(
      true,
    );
    expect(startJob).not.toHaveBeenCalled();
  });

  it('blocks submission on a browser that cannot mint a durable request id', async () => {
    const realCrypto = globalThis.crypto;
    Object.defineProperty(globalThis, 'crypto', { configurable: true, value: {} });
    try {
      const { startJob } = await renderPanel();

      expect(screen.getByText('This browser cannot create a durable request identity.')).toBeTruthy();
      expect(
        (screen.getByRole('button', { name: 'Run backfill' }) as HTMLButtonElement).disabled,
      ).toBe(true);
      expect(startJob).not.toHaveBeenCalled();
    } finally {
      Object.defineProperty(globalThis, 'crypto', { configurable: true, value: realCrypto });
    }
  });

  it('accepts a window exactly at the range cap', async () => {
    // 1830 inclusive days from 2026-01-01 is 2031-01-04.
    const { startJob } = await renderPanel({
      seedStartTradingDate: '2026-01-01',
      seedEndTradingDate: '2031-01-04',
    });

    fireEvent.click(screen.getByRole('button', { name: 'Run backfill' }));
    await vi.waitFor(() => expect(startJob).toHaveBeenCalled());
  });

  it('blocks a window one day past the cap instead of letting the server refuse it', async () => {
    const { startJob } = await renderPanel({
      seedStartTradingDate: '2026-01-01',
      seedEndTradingDate: '2031-01-05',
    });

    expect(
      screen.getByText('That window is 1831 days; the data plane accepts at most 1830.'),
    ).toBeTruthy();
    expect(
      (screen.getByRole('button', { name: 'Run backfill' }) as HTMLButtonElement).disabled,
    ).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: 'Run backfill' }));
    expect(startJob).not.toHaveBeenCalled();
  });

  it('honours a cap the data plane lowered rather than a hardcoded one', async () => {
    await renderPanel({
      defaults: { ...DEFAULTS, max_trading_range_days: 30 },
      seedStartTradingDate: '2026-05-01',
      seedEndTradingDate: '2026-05-31',
    });

    expect(
      screen.getByText('That window is 31 days; the data plane accepts at most 30.'),
    ).toBeTruthy();
  });

  it('refuses to backfill while the lean_adjusted view is selected', async () => {
    // Nothing derives lean_adjusted — it would come from raw bars plus
    // factor files and no producer exists — so the job would succeed and
    // leave the selected view exactly as empty as it started.
    const { startJob } = await renderPanel({ priceAdjustmentMode: 'lean_adjusted' });

    expect(screen.getByText(/Nothing derives/)).toBeTruthy();
    expect(
      (screen.getByRole('button', { name: 'Run backfill' }) as HTMLButtonElement).disabled,
    ).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: 'Run backfill' }));
    expect(startJob).not.toHaveBeenCalled();
  });

  it.each(['raw', 'polygon_split_adjusted'] as const)(
    'backfills the %s view into that view, not into raw',
    async (mode) => {
      // #1839 gave the lake a root per adjustment mode and widened the fetch
      // pipeline to produce polygon_split_adjusted. An operator looking at
      // missing adjusted coverage must be able to fill it from here, and the
      // spec must name the mode or the rows would land in the raw tree.
      const { startJob } = await renderPanel({ priceAdjustmentMode: mode });

      fireEvent.click(screen.getByRole('button', { name: 'Run backfill' }));

      await vi.waitFor(() => expect(startJob).toHaveBeenCalled());
      const [, payload] = startJob.mock.calls[0] as [string, { spec: Record<string, unknown> }];
      expect(payload.spec['price_adjustment_mode']).toBe(mode);
    },
  );

  it('attributes each failure to its symbol when several fail the same way', async () => {
    // Two symbols, same session, same reason: identical in (date, kind,
    // reason). Without the symbol in the key Angular rejects the duplicate
    // identity, and without it on screen the receipt cannot be acted on.
    const { store, detectChanges } = await renderPanel();
    fireEvent.click(screen.getByRole('button', { name: 'Run backfill' }));
    await vi.waitFor(() => expect(store.jobId()).toBe('job-77'));

    store.ingestEvent({
      type: 'data_lake.backfill_day',
      trading_date_ms: MAY_20_OPEN_MS,
      day_index: 1,
      total_days: 1,
      days_remaining: 0,
      fetched_count: 0,
      reused_count: 0,
      failures: [fakeFailure('SPY'), fakeFailure('AAPL')],
    });
    detectChanges();

    const failures = screen.getByRole('list', { name: 'Backfill failures' });
    expect(within(failures).getAllByRole('listitem')).toHaveLength(2);
    expect(within(failures).getByText('SPY')).toBeTruthy();
    expect(within(failures).getByText('AAPL')).toBeTruthy();
  });

  it('says a failure is not symbol-scoped rather than leaving it unattributed', async () => {
    const { store, detectChanges } = await renderPanel();
    fireEvent.click(screen.getByRole('button', { name: 'Run backfill' }));
    await vi.waitFor(() => expect(store.jobId()).toBe('job-77'));

    store.ingestEvent({
      type: 'data_lake.backfill_day',
      trading_date_ms: MAY_20_OPEN_MS,
      day_index: 1,
      total_days: 1,
      days_remaining: 0,
      fetched_count: 0,
      reused_count: 0,
      failures: [{ ...fakeFailure('SPY'), symbol: null, reason: 'run_aborted' }],
    });
    detectChanges();

    expect(screen.getByText('whole run')).toBeTruthy();
  });

  it('adopts a backfill that was already running when the panel mounted', async () => {
    const { store, fixture, detectChanges } = await renderPanel({
      liveJobs: [{ id: 'job-live', type: 'data_lake_backfill', status: 'running' }],
    });
    const finished = vi.fn();
    fixture.componentInstance.runFinished.subscribe(finished);

    await vi.waitFor(() => expect(store.jobId()).toBe('job-live'));
    expect(screen.getByText(/Reattached to a backfill that was already running/)).toBeTruthy();

    store.ingestEvent({ type: 'job.completed' });
    detectChanges();

    expect(finished).toHaveBeenCalledTimes(1);
  });

  it('leaves an unrelated or finished job alone', async () => {
    const { store } = await renderPanel({
      liveJobs: [
        { id: 'other-type', type: 'engine_backtest', status: 'running' },
        { id: 'already-done', type: 'data_lake_backfill', status: 'completed' },
      ],
    });

    await vi.waitFor(() => expect(store.phase()).toBe('idle'));
    expect(store.jobId()).toBeNull();
  });

  it("names a terminal job failure with the framework's own code", async () => {
    const { store, detectChanges } = await renderPanel();
    fireEvent.click(screen.getByRole('button', { name: 'Run backfill' }));
    await vi.waitFor(() => expect(store.jobId()).toBe('job-77'));

    store.ingestEvent({
      type: 'job.failed',
      code: 'fetch_timeout',
      message: 'Polygon did not answer within 600s.',
    });
    detectChanges();

    expect(screen.getByText('Fetch Timeout')).toBeTruthy();
    expect(screen.getByText('Polygon did not answer within 600s.')).toBeTruthy();
  });

  it('passes AXE', async () => {
    await renderPanel();

    const results = await axe.run(document.body, {
      rules: { 'color-contrast': { enabled: false }, region: { enabled: false } },
    });

    expect(results.violations).toEqual([]);
  });
});
