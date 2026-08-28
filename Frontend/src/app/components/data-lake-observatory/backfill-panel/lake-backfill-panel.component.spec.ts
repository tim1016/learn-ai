import { fireEvent, render, screen } from '@testing-library/angular';
import axe from 'axe-core';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { JobsService } from '../../../services/jobs.service';
import { DataLakeBackfillStore } from '../lib/data-lake-backfill.store';
import type { BackfillDefaults } from '../lib/data-lake.types';
import { LakeBackfillPanelComponent } from './lake-backfill-panel.component';

/** 09:30 America/New_York on 2026-05-20, as int64 ms UTC. */
const MAY_20_OPEN_MS = Date.UTC(2026, 4, 20, 13, 30);

const DEFAULTS: BackfillDefaults = {
  market: 'usa',
  lean_image_digest: 'sha256:pinned',
  max_trading_range_days: 1830,
  max_symbol_length: 20,
};

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

async function renderPanel(defaults: BackfillDefaults | null = DEFAULTS) {
  const startJob = vi.fn().mockResolvedValue('job-77');
  const cancelJob = vi.fn().mockResolvedValue(undefined);
  const view = await render(LakeBackfillPanelComponent, {
    providers: [{ provide: JobsService, useValue: { startJob, cancelJob } }],
    componentInputs: {
      defaults,
      seedSymbols: 'SPY',
      seedStartTradingDate: '2026-05-18',
      seedEndTradingDate: '2026-05-22',
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
      force_refresh: false,
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

  it('tells the page a run finished so coverage can be re-read', async () => {
    const { store, fixture, detectChanges } = await renderPanel();
    const finished = vi.fn();
    fixture.componentInstance.runFinished.subscribe(finished);

    fireEvent.click(screen.getByRole('button', { name: 'Run backfill' }));
    await vi.waitFor(() => expect(store.jobId()).toBe('job-77'));
    store.ingestEvent({ type: 'job.completed' });
    detectChanges();

    expect(finished).toHaveBeenCalledTimes(1);
  });

  it('refuses to submit without a pinned LEAN image digest', async () => {
    const { startJob } = await renderPanel({ ...DEFAULTS, lean_image_digest: null });

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
