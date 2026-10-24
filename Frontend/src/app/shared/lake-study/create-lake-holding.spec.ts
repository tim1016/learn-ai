import { signal, type WritableSignal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { describe, expect, it, vi } from 'vitest';

import { DataLakeService, type DataLakeRead, type InstrumentDataReport, type InstrumentDataRequest } from '../data-lake';
import type { BackfillJobRun, BackfillRunState } from '../data-lake/backfill-job-runner';
import { heldReport } from '../data-lake/testing/instrument-data-report';
import { TradeBackfillRunner, type TradeBackfillEnd } from '../data-lake/trade-backfill-runner';
import type { TradeBackfillWindow } from '../data-lake/trade-backfill-spec';
import { fakeTickerCatalog, provideFakeTickerCatalog } from '../ticker-catalog/testing/fake-ticker-catalog';
import { createLakeHolding, type LakeHoldingControl } from './create-lake-holding';

const REQUEST: InstrumentDataRequest = { symbol: 'SPY', from: '2024-03-04', to: '2024-03-15', tree: 'polygon_split_adjusted' };
const LACKING: InstrumentDataReport = heldReport({ sessions: 60, missing: 12 });
const SPAN = LACKING.fill_window;

/** The shared backfill runner as a page meets it: it takes the window the page names, hands over a job, and ends when told to. */
function fakeRunner() {
  const state: WritableSignal<BackfillRunState> = signal({ kind: 'running', started: true, progress: null });
  const job = { jobId: 'job-1', state, terminal: new Promise<never>(() => undefined), detach: vi.fn(), cancel: vi.fn().mockResolvedValue(undefined) };
  let end!: (result: TradeBackfillEnd) => void;
  let asked: TradeBackfillWindow | null = null;
  const run = vi.fn(async (select: () => TradeBackfillWindow, accepted: (job: BackfillJobRun) => boolean | Promise<boolean>) => {
    asked = select();
    await accepted(job as unknown as BackfillJobRun);
    return new Promise<TradeBackfillEnd>((resolve) => (end = resolve));
  });
  return { run, job, state, end: (result: TradeBackfillEnd) => end(result), asked: () => asked };
}

function setup(first: DataLakeRead<InstrumentDataReport> = { kind: 'ok', value: LACKING }) {
  const instrumentData = vi.fn().mockResolvedValue(first);
  const runner = fakeRunner();
  const catalog = fakeTickerCatalog();
  TestBed.configureTestingModule({
    providers: [
      { provide: DataLakeService, useValue: { instrumentData } },
      { provide: TradeBackfillRunner, useValue: { run: runner.run } },
      provideFakeTickerCatalog(catalog),
    ],
  });
  const request = signal<InstrumentDataRequest | null>(REQUEST);
  const control: LakeHoldingControl = TestBed.runInInjectionContext(() => createLakeHolding(request));
  const settle = async (kind: string) => {
    TestBed.tick();
    await vi.waitFor(() => expect(control.reading().kind).toBe(kind));
  };
  return { instrumentData, runner, catalog, request, control, settle };
}

describe('createLakeHolding', () => {
  it('reads the count for the span, once, and again only when the instrument, the window or the tree changes', async () => {
    const { instrumentData, request, control, settle } = setup();

    await settle('read');
    expect(instrumentData).toHaveBeenCalledTimes(1);
    expect(instrumentData).toHaveBeenCalledWith(REQUEST);

    request.set({ ...REQUEST });
    TestBed.tick();
    expect.soft(instrumentData, 'an equal request built again reads nothing').toHaveBeenCalledTimes(1);

    request.set({ ...REQUEST, to: '2024-03-14' });
    TestBed.tick();
    await vi.waitFor(() => expect(instrumentData).toHaveBeenCalledTimes(2));
    expect(control.reading().kind).not.toBe('idle');
  });

  it('reads nothing while the inputs are incomplete', async () => {
    const { instrumentData, request, control } = setup();
    request.set(null);

    TestBed.tick();

    expect.soft(control.reading()).toEqual({ kind: 'idle' });
    expect.soft(instrumentData).not.toHaveBeenCalled();
  });

  it('maps the lake’s counts, and starts no capture by reading them', async () => {
    const { runner, control, settle } = setup();

    await settle('read');

    const reading = control.reading();
    expect.soft(reading.kind === 'read' && reading.holding.toCapture).toBe(12);
    expect.soft(runner.run).not.toHaveBeenCalled();
    expect.soft(control.capture()).toEqual({ kind: 'idle' });
  });

  it.each([
    ['a typed refusal', { kind: 'rejected', reason: 'invalid_trading_date', message: 'Pick a start and end date in YYYY-MM-DD form.' } as const, 'Pick a start and end date'],
    ['an unreachable data plane', { kind: 'unavailable', message: 'The data plane did not respond.' } as const, 'The data plane did not respond.'],
  ])('says a count it could not read is unreadable, with the lake’s words, for %s', async (_name, failure, words) => {
    const { control, settle } = setup(failure);

    await settle('unreadable');

    const reading = control.reading();
    expect(reading.kind === 'unreadable' && reading.message).toContain(words);
  });

  describe('startCapture', () => {
    it('asks the shared runner for exactly the span the count named, shows its progress, and reads the count again when it ends', async () => {
      const { instrumentData, runner, catalog, control, settle } = setup();
      await settle('read');

      const started = control.startCapture();
      await vi.waitFor(() => expect(control.capture().kind).toBe('running'));

      expect.soft(runner.asked()).toEqual({
        symbol: 'SPY', price_adjustment_mode: 'polygon_split_adjusted',
        start_trading_date_ms: SPAN?.start_trading_date_ms, end_trading_date_ms: SPAN?.end_trading_date_ms,
      });
      expect.soft(control.capture()).toEqual({ kind: 'running', symbol: 'SPY', sessions: 12, cancelRefused: null });
      expect.soft(control.progress()).toBeNull();
      runner.state.set({ kind: 'running', started: true, progress: { current: 5, total: 20, unit: 'days', message: null } });
      expect.soft(control.progress()).toEqual({ done: 5, total: 20 });

      runner.end({ kind: 'completed', failure: null });
      await started;
      TestBed.tick();

      expect.soft(control.capture()).toEqual({ kind: 'idle' });
      expect.soft(control.progress()).toBeNull();
      await vi.waitFor(() => expect(instrumentData).toHaveBeenCalledTimes(2));
      expect.soft(catalog.viewFor('polygon_split_adjusted').reloadCount, 'the pickers’ held spans are read again').toBe(1);
    });

    it('drops a second press while one capture runs', async () => {
      const { runner, control, settle } = setup();
      await settle('read');

      const first = control.startCapture();
      await vi.waitFor(() => expect(control.capture().kind).toBe('running'));
      await control.startCapture();
      runner.end({ kind: 'completed', failure: null });
      await first;

      expect(runner.run).toHaveBeenCalledTimes(1);
    });

    it('starts nothing when the count names no session to capture, or has not been read', async () => {
      const { runner, control, settle } = setup({ kind: 'ok', value: heldReport({ sessions: 60, beforeProviderHistory: 3 }) });

      await control.startCapture();
      await settle('read');
      await control.startCapture();

      expect(runner.run).not.toHaveBeenCalled();
    });

    it.each([
      ['fails', { kind: 'failed', code: 'backfill_failed', message: 'the provider refused the key' } as const, false, 'the provider refused the key'],
      ['is cancelled', { kind: 'cancelled', code: 'backfill_cancelled', message: 'The backfill was cancelled.' } as const, true, 'The backfill was cancelled.'],
    ])('keeps a capture that %s in view until it is dismissed', async (_name, end, cancelled, message) => {
      const { runner, control, settle } = setup();
      await settle('read');

      const started = control.startCapture();
      await vi.waitFor(() => expect(control.capture().kind).toBe('running'));
      runner.end(end);
      await started;

      const stopped = control.capture();
      expect.soft(stopped.kind === 'stopped' && stopped.cancelled).toBe(cancelled);
      expect.soft(stopped.kind === 'stopped' && stopped.message).toContain(message);
      control.dismissCapture();
      expect.soft(control.capture()).toEqual({ kind: 'idle' });
    });

    it('returns to idle without a note when the page stops watching the capture', async () => {
      const { runner, control, settle } = setup();
      await settle('read');

      const started = control.startCapture();
      await vi.waitFor(() => expect(control.capture().kind).toBe('running'));
      runner.end({ kind: 'detached' });
      await started;

      expect(control.capture()).toEqual({ kind: 'idle' });
    });
  });

  describe('cancelCapture', () => {
    it('asks the server to cancel the running job', async () => {
      const { runner, control, settle } = setup();
      await settle('read');
      void control.startCapture();
      await vi.waitFor(() => expect(control.capture().kind).toBe('running'));

      await control.cancelCapture();

      expect(runner.job.cancel).toHaveBeenCalledOnce();
    });

    it('stays running when the server could not be told, says why beside it, and offers Cancel again', async () => {
      const { runner, control, settle } = setup();
      await settle('read');
      void control.startCapture();
      await vi.waitFor(() => expect(control.capture().kind).toBe('running'));
      runner.job.cancel.mockRejectedValueOnce(new Error('Network error'));

      await control.cancelCapture();

      const refused = control.capture();
      expect.soft(refused.kind, 'the job may still be writing').toBe('running');
      expect.soft(refused.kind === 'running' && refused.cancelRefused).toBe('The server could not be told to stop it, so it may still be running. Network error');
      expect.soft(refused.kind === 'running' && refused.sessions, 'it is still the same capture').toBe(12);
      control.dismissCapture();
      expect.soft(control.capture().kind, 'there is no note to put away while it runs').toBe('running');

      await control.cancelCapture();

      expect.soft(runner.job.cancel, 'Cancel is offered again').toHaveBeenCalledTimes(2);
    });

    it('starts no second capture after a cancel the server refused, because the first job may still be writing', async () => {
      const { runner, control, settle } = setup();
      await settle('read');
      const first = control.startCapture();
      await vi.waitFor(() => expect(control.capture().kind).toBe('running'));
      runner.job.cancel.mockRejectedValueOnce(new Error('Network error'));
      await control.cancelCapture();
      control.dismissCapture();

      await control.startCapture();

      expect.soft(runner.run, 'the first job has not ended').toHaveBeenCalledTimes(1);
      runner.end({ kind: 'completed', failure: null });
      await first;
      expect.soft(control.capture(), 'the refusal belongs to the job that ended').toEqual({ kind: 'idle' });
      void control.startCapture();
      await vi.waitFor(() => expect(runner.run, 'once it has ended the person may capture again').toHaveBeenCalledTimes(2));
      expect(control.capture()).toEqual({ kind: 'running', symbol: 'SPY', sessions: 12, cancelRefused: null });
      runner.end({ kind: 'completed', failure: null });
    });
  });

  it('reads the count again when the page asks, for a run that has just filled the lake', async () => {
    const { instrumentData, control, settle } = setup();
    await settle('read');

    control.reread();
    TestBed.tick();

    await vi.waitFor(() => expect(instrumentData).toHaveBeenCalledTimes(2));
  });
});
