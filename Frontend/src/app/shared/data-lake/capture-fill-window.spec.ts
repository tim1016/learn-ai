import { HttpErrorResponse } from '@angular/common/http';
import { EnvironmentInjector, createEnvironmentInjector, runInInjectionContext, signal, type WritableSignal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { describe, expect, it, vi } from 'vitest';

import { fakeTickerCatalog, provideFakeTickerCatalog } from '../ticker-catalog/testing/fake-ticker-catalog';
import type { BackfillJobRun, BackfillRunState } from './backfill-job-runner';
import { createFillWindowCapture, type FillWindow } from './capture-fill-window';
import { TradeBackfillRunner, type TradeBackfillEnd } from './trade-backfill-runner';
import type { TradeBackfillWindow } from './trade-backfill-spec';

const WINDOW: FillWindow = { start_trading_date_ms: Date.UTC(2024, 2, 4, 12), end_trading_date_ms: Date.UTC(2024, 2, 15, 12) };
const SUBJECT = { symbol: 'SPY', tree: 'polygon_split_adjusted' } as const;

/** The shared backfill runner as a capture meets it: it takes the window named, hands over a job, and ends when told to. */
function setup(options: { acceptAfter?: Promise<void> } = {}) {
  const state: WritableSignal<BackfillRunState> = signal({ kind: 'running', started: true, progress: null });
  const job = { jobId: 'job-1', state, terminal: new Promise<never>(() => undefined), detach: vi.fn(), cancel: vi.fn().mockResolvedValue(undefined) };
  let end!: (result: TradeBackfillEnd) => void;
  let asked: TradeBackfillWindow | null = null;
  const run = vi.fn(async (select: () => TradeBackfillWindow, accepted: (job: BackfillJobRun) => boolean | Promise<boolean>) => {
    asked = select();
    await options.acceptAfter;
    if (!(await accepted(job as unknown as BackfillJobRun))) return { kind: 'detached' } satisfies TradeBackfillEnd;
    return new Promise<TradeBackfillEnd>((resolve) => (end = resolve));
  });
  const catalog = fakeTickerCatalog();
  TestBed.configureTestingModule({ providers: [{ provide: TradeBackfillRunner, useValue: { run } }, provideFakeTickerCatalog(catalog)] });
  const page = createEnvironmentInjector([], TestBed.inject(EnvironmentInjector));
  const capture = runInInjectionContext(page, () => createFillWindowCapture());
  const reloads = () => catalog.viewFor('polygon_split_adjusted').reloadCount;
  return { capture, page, job, state, run, catalog, reloads, asked: () => asked, finish: (result: TradeBackfillEnd) => end(result) };
}

describe('createFillWindowCapture', () => {
  it('captures exactly the window the server named, is busy and shows the job’s progress while it runs, and reads the catalog again when it ends', async () => {
    const { capture, state, reloads, asked, finish, run } = setup();

    const running = capture.run(SUBJECT, WINDOW);
    await vi.waitFor(() => expect(run).toHaveBeenCalled());
    await vi.waitFor(() => expect(capture.busy()).toBe(true));

    expect.soft(asked()).toEqual({
      symbol: 'SPY', price_adjustment_mode: 'polygon_split_adjusted',
      start_trading_date_ms: WINDOW.start_trading_date_ms, end_trading_date_ms: WINDOW.end_trading_date_ms,
    });
    expect.soft(capture.progress()).toBeNull();
    state.set({ kind: 'running', started: true, progress: { current: 5, total: 20, unit: 'days', message: null } });
    expect.soft(capture.progress()).toEqual({ done: 5, total: 20 });
    expect.soft(reloads(), 'nothing is read again while the job runs').toBe(0);

    finish({ kind: 'completed', failure: null });

    expect(await running).toEqual({ kind: 'completed', failure: null });
    expect.soft(capture.busy()).toBe(false);
    expect.soft(capture.progress()).toBeNull();
    expect.soft(reloads(), 'the pickers’ held spans are read off the lake the job wrote to').toBe(1);
  });

  it.each([
    ['fails', { kind: 'failed', code: 'backfill_failed', message: 'the provider refused the key' } as const],
    ['is cancelled', { kind: 'cancelled', code: 'backfill_cancelled', message: 'The backfill was cancelled.' } as const],
  ])('reads the catalog again when the job %s, because it wrote what it wrote before it stopped', async (_name, result) => {
    const { capture, reloads, finish, run } = setup();

    const running = capture.run(SUBJECT, WINDOW);
    await vi.waitFor(() => expect(run).toHaveBeenCalled());
    await vi.waitFor(() => expect(capture.busy()).toBe(true));
    finish(result);

    expect(await running).toEqual(result);
    expect(reloads()).toBe(1);
  });

  it('says why the server could not be told to stop the job, and stays busy until the job has ended', async () => {
    const { capture, job, finish, run } = setup();
    const running = capture.run(SUBJECT, WINDOW);
    await vi.waitFor(() => expect(run).toHaveBeenCalled());
    await vi.waitFor(() => expect(capture.busy()).toBe(true));
    job.cancel.mockRejectedValueOnce(new Error('Network error'));

    const refused = await capture.cancel();

    expect.soft(refused).toBe('The server could not be told to stop it, so it may still be running. Network error');
    expect.soft(capture.busy(), 'the job may still be writing').toBe(true);
    expect.soft(await capture.cancel(), 'a cancel the server took has nothing to say').toBeNull();
    finish({ kind: 'cancelled', code: 'backfill_cancelled', message: 'The backfill was cancelled.' });
    await running;
    expect(capture.busy()).toBe(false);
  });

  it('words a cancel the server answered with an error in the server’s own words, not as an object', async () => {
    const { capture, job, finish, run } = setup();
    const running = capture.run(SUBJECT, WINDOW);
    await vi.waitFor(() => expect(run).toHaveBeenCalled());
    await vi.waitFor(() => expect(capture.busy()).toBe(true));
    job.cancel.mockRejectedValueOnce(new HttpErrorResponse({
      status: 409,
      statusText: 'Conflict',
      error: { detail: { reason: 'job_not_cancellable', message: 'The job already finished.' } },
    }));

    const refused = await capture.cancel();

    expect.soft(refused).toBe('The server could not be told to stop it, so it may still be running. The job already finished.');
    expect.soft(refused).not.toContain('[object Object]');
    finish({ kind: 'cancelled', code: 'backfill_cancelled', message: 'The backfill was cancelled.' });
    await running;
  });

  it('stops watching the job, and never stops it, when the page is left, and reads nothing for a page that has gone', async () => {
    const { capture, page, job, reloads, finish, run } = setup();
    const running = capture.run(SUBJECT, WINDOW);
    await vi.waitFor(() => expect(run).toHaveBeenCalled());
    await vi.waitFor(() => expect(capture.busy()).toBe(true));

    page.destroy();
    finish({ kind: 'completed', failure: null });

    expect.soft(job.detach).toHaveBeenCalledOnce();
    expect.soft(job.cancel).not.toHaveBeenCalled();
    expect.soft(await running, 'a page that has gone is told nothing it could act on').toEqual({ kind: 'detached' });
    expect.soft(reloads()).toBe(0);
  });

  it('lets go of a job that was accepted after the page had gone, instead of leaving its stream open', async () => {
    let release!: () => void;
    const { capture, page, job, run } = setup({ acceptAfter: new Promise<void>((resolve) => (release = resolve)) });
    const running = capture.run(SUBJECT, WINDOW);
    await vi.waitFor(() => expect(run).toHaveBeenCalled());

    page.destroy();
    release();

    expect.soft(await running).toEqual({ kind: 'detached' });
    expect.soft(job.detach, 'the page owns the handle once it is accepted; refusing it means it must be let go').toHaveBeenCalled();
    expect.soft(job.cancel).not.toHaveBeenCalled();
  });
});
