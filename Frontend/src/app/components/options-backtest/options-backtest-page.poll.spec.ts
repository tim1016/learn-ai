import { fireEvent, screen, waitFor, within } from '@testing-library/angular';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { fakeJobs } from './testing/fake-jobs';
import { openExample, pickDates } from './testing/page-actions';
import { fakeService, renderPage } from './testing/render-page';
import { runDetail, runItem } from './testing/run-fixtures';
import { goingDetail as going, scriptedRun } from './testing/run-script';

/**
 * The run's record is the truth, and the page reads it again every few seconds while it says `running`, so a backtest
 * the stream lost track of ends on the page all the same: the result or the reason is shown, Cancel goes away, and
 * Start comes free. Under fake timers that also move with the clock, so no spec asks for a read at the edge of an interval.
 */
const POLL_MS = 3_000;

beforeEach(() => vi.useFakeTimers({ shouldAdvanceTime: true }));
afterEach(() => vi.useRealTimers());

const after = (ms: number) => vi.advanceTimersByTimeAsync(ms);
const startButton = () => screen.getByRole<HTMLButtonElement>('button', { name: 'Start backtest' });
const over = (changes: Parameters<typeof runDetail>[0]) => runDetail({ figures: null, slippage: [], slippage_tolerance: null, headline: null, ...changes });

describe('Start comes free when the record says the backtest is over', () => {
  it('after the page came back to a run whose job it could not find', async () => {
    const jobs = fakeJobs();
    const service = fakeService(vi, { runs: vi.fn(async () => [runItem()]) });
    service.run.mockResolvedValueOnce(going()).mockResolvedValueOnce(going()).mockResolvedValue(over({ status: 'failed', failure: { code: 'DATA_MISSING', reason: 'Some sessions have no data to replay.', items: [] } }));
    await renderPage(service, undefined, { jobs, polling: true });
    const panel = await screen.findByRole('region', { name: 'Backtest run' });
    await openExample();
    pickDates('2026-09-14', '2026-09-18');
    await within(panel).findByText('Waiting for the run’s progress');

    await waitFor(() => expect(screen.getByText('A backtest is already running.')).toBeTruthy());
    expect(startButton().disabled).toBe(true);
    await after(POLL_MS);
    await waitFor(() => expect(service.run).toHaveBeenCalledTimes(2));
    expect(startButton().disabled).toBe(true);
    await after(POLL_MS);

    expect(await within(panel).findByText('Some sessions have no data to replay.')).toBeTruthy();
    expect(within(panel).queryByText('Waiting for the run’s progress')).toBeNull();
    await waitFor(() => expect(startButton().disabled).toBe(false));
    expect(screen.queryByText('A backtest is already running.')).toBeNull();
  });

});

describe('once the job has started, not finding its run never says the backtest was not started, and never frees Start', () => {
  const shiftEnter = () => document.body.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', shiftKey: true, bubbles: true, cancelable: true }));

  it.each([
    ['cannot be read', () => Promise.reject(new Error('offline'))],
    ['is not in the list yet', () => Promise.resolve([])],
  ])('when the record %s', async (_case, firstLookup) => {
    const setup = scriptedRun(vi);
    let lookups = 0;
    setup.service.runs.mockImplementation(async (query) => {
      if (query?.jobId === undefined) return [];
      lookups += 1;
      return lookups === 1 ? firstLookup() : [runItem({ job_id: 'job-1' })];
    });
    await renderPage(setup.service, undefined, { jobs: setup.jobs, polling: true });
    await openExample();
    pickDates('2026-09-14', '2026-09-18');
    await waitFor(() => expect(startButton().disabled).toBe(false));

    fireEvent.click(startButton());
    const notice = await screen.findByText(/The backtest was started, but its record cannot be read yet/);

    expect(screen.queryByText(/could not be started/)).toBeNull();
    expect(notice.closest('[role="status"]')).not.toBeNull();
    expect(startButton().disabled).toBe(true);
    expect(screen.getByText('A backtest is already running.')).toBeTruthy();
    fireEvent.click(startButton());
    shiftEnter();
    shiftEnter();
    await after(1_000);
    expect(setup.service.startRun).toHaveBeenCalledTimes(1);

    await after(POLL_MS);
    expect(await screen.findByRole('region', { name: 'Backtest run' })).toBeTruthy();
    expect(screen.queryByText(/The backtest was started, but its record/)).toBeNull();
    expect(startButton().disabled).toBe(true);
    expect(setup.service.startRun).toHaveBeenCalledTimes(1);
  });
});
