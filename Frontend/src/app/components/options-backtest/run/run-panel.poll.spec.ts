import { screen, waitFor, within } from '@testing-library/angular';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { RunDetail, RunListItem } from '../options-backtest.types';
import { fakeJobs } from '../testing/fake-jobs';
import { fakeService } from '../testing/render-page';
import { readsAs, renderRunPanel } from '../testing/render-run';
import { runDetail, runItem } from '../testing/run-fixtures';

/**
 * The run's record is the truth, and the panel reads it again every few seconds for as long as it says `running`; the
 * job's stream only decorates it and says sooner when the job is over. These run under fake timers that also move with
 * the clock (so the page can draw), which is why no spec asks for a read at the very edge of an interval.
 */
const POLL_MS = 3_000;

beforeEach(() => vi.useFakeTimers({ shouldAdvanceTime: true }));
afterEach(() => vi.useRealTimers());

const renderRun = (run: RunListItem, options: Parameters<typeof renderRunPanel>[2] = {}) => renderRunPanel(vi, run, { polling: true, ...options });
const after = (ms: number) => vi.advanceTimersByTimeAsync(ms);
const without = (changes: Partial<RunDetail>): RunDetail => runDetail({ figures: null, slippage: [], slippage_tolerance: null, headline: null, ...changes });
const going = (): RunDetail => without({ status: 'running', finished_at_ms: null });
const cancelButton = () => screen.queryByRole('button', { name: /^Cancel/ });

describe('a run that is going, read again while its record says so', () => {
  it('reads it again every few seconds, shows what the record says, and stops asking once it says anything else', async () => {
    const service = fakeService(vi);
    readsAs(service, going(), going(), runDetail());
    const { updated } = await renderRun(runItem(), { service });
    await screen.findByRole('button', { name: 'Cancel' });
    expect(service.run).toHaveBeenCalledTimes(1);

    await after(1_000);
    expect(service.run).toHaveBeenCalledTimes(1);
    await after(POLL_MS);
    await waitFor(() => expect(service.run).toHaveBeenCalledTimes(2));
    expect(cancelButton()).not.toBeNull();
    await after(POLL_MS);

    expect(await screen.findByRole('region', { name: 'Headline figures' })).toBeTruthy();
    expect(cancelButton()).toBeNull();
    expect(updated.map((detail) => detail.status)).toEqual(['complete']);
    await after(10 * POLL_MS);
    expect(service.run).toHaveBeenCalledTimes(3);
  });

  it('reads the run it is given now, and never the one it was given before, when another run takes its place while the poll of the first is waiting', async () => {
    const service = fakeService(vi);
    service.run.mockImplementation((id: number) => (id === 7 ? Promise.resolve(going()) : new Promise<RunDetail>(() => undefined)));
    const { view } = await renderRun(runItem({ id: 7, job_id: 'job-7' }), { service });
    await screen.findByRole('button', { name: 'Cancel' });
    expect(service.run.mock.calls.map(([id]) => id)).toEqual([7]);

    view.fixture.componentRef.setInput('run', runItem({ id: 8, job_id: 'job-8' }));
    await waitFor(() => expect(service.run.mock.calls.map(([id]) => id)).toEqual([7, 8]));
    await after(2 * POLL_MS);

    const asked = service.run.mock.calls.map(([id]) => id);
    expect(asked.filter((id) => id === 7)).toHaveLength(1);
    expect(asked.length).toBeGreaterThanOrEqual(3);
  });

  it('does not read again a run that is already over', async () => {
    const service = fakeService(vi);
    readsAs(service, runDetail());
    await renderRun(runItem({ status: 'complete' }), { service });
    await screen.findByRole('region', { name: 'Headline figures' });

    await after(5 * POLL_MS);

    expect(service.run).toHaveBeenCalledTimes(1);
  });

  it('stops reading when the panel is gone', async () => {
    const service = fakeService(vi);
    readsAs(service, going());
    const { view } = await renderRun(runItem(), { service });
    await screen.findByRole('button', { name: 'Cancel' });

    view.fixture.destroy();
    await after(5 * POLL_MS);

    expect(service.run).toHaveBeenCalledTimes(1);
  });

  it('starts no poll from a read that comes back after the panel is gone', async () => {
    const service = fakeService(vi);
    const answers: ((detail: RunDetail) => void)[] = [];
    service.run.mockImplementation(() => new Promise<RunDetail>((resolve) => answers.push(resolve)));
    const { view } = await renderRun(runItem(), { service });
    await waitFor(() => expect(answers).toHaveLength(1));

    view.fixture.destroy();
    answers[0]?.(going());
    await after(5 * POLL_MS);

    expect(service.run).toHaveBeenCalledTimes(1);
  });
});

describe('when the job and the record do not agree', () => {
  it('reads the record again when the job is over but the record still says running, and ends on what it says next', async () => {
    const service = fakeService(vi);
    readsAs(service, going(), going(), runDetail());
    const jobs = fakeJobs();
    jobs.register('job-7');
    const { updated } = await renderRun(runItem(), { service, jobs });
    jobs.emit('job-7', { type: 'job.phase', phase: 'saving', friendly: 'Saving the run' });
    await screen.findByText('Saving the run');

    jobs.emit('job-7', { type: 'job.completed', result_url: '/x' });
    await waitFor(() => expect(service.run).toHaveBeenCalledTimes(2));
    await lettingGoOnce();

    expect(cancelButton()).not.toBeNull();
    expect(screen.queryByRole('region', { name: 'Headline figures' })).toBeNull();
    await after(POLL_MS);
    expect(await screen.findByRole('region', { name: 'Headline figures' })).toBeTruthy();
    expect(cancelButton()).toBeNull();
    expect(updated.map((detail) => detail.status)).toEqual(['complete']);
  });

  it('turns into the saved result when the save landed after all, though the job ended saying the save timed out', async () => {
    const service = fakeService(vi);
    readsAs(service, going(), going(), runDetail());
    const jobs = fakeJobs();
    jobs.register('job-7');
    await renderRun(runItem(), { service, jobs });

    jobs.emit('job-7', { type: 'job.failed', code: 'SAVE_TIMED_OUT', message: 'The result may or may not have been saved.' });
    await waitFor(() => expect(service.run).toHaveBeenCalledTimes(2));
    await after(POLL_MS);

    expect(await screen.findByRole('region', { name: 'Headline figures' })).toBeTruthy();
    expect(screen.queryByText(/Save Timed Out/)).toBeNull();
    expect(cancelButton()).toBeNull();
  });

  it('shows how the run ended when the job vanished with no word, once the record says so', async () => {
    const failure = { code: 'INTERNAL_ERROR', reason: 'The run stopped for a reason of its own.', items: [] };
    const service = fakeService(vi);
    readsAs(service, going(), going(), without({ status: 'failed', failure }));
    const jobs = fakeJobs();
    jobs.register('job-7');
    await renderRun(runItem(), { service, jobs });
    jobs.emit('job-7', { type: 'job.phase', phase: 'replaying', friendly: 'Replaying each session' });
    await screen.findByText('Replaying each session');

    await after(POLL_MS);
    await after(POLL_MS);

    expect(await screen.findByText('The run stopped for a reason of its own.')).toBeTruthy();
    expect(cancelButton()).toBeNull();
    expect(screen.queryByText('Replaying each session')).toBeNull();
  });

  it('says it is waiting for the progress of a job this tab cannot find, asks for the jobs again at each read, and ends on what the record says', async () => {
    const service = fakeService(vi);
    readsAs(service, going(), going(), going(), runDetail());
    const jobs = fakeJobs();
    const { updated } = await renderRun(runItem(), { service, jobs });

    expect(await screen.findByText('Waiting for the run’s progress')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeTruthy();
    expect(jobs.refreshes).toBe(1);
    await after(POLL_MS);
    await waitFor(() => expect(jobs.refreshes).toBe(2));
    await after(POLL_MS);
    await waitFor(() => expect(jobs.refreshes).toBe(3));
    expect(screen.getByText('Waiting for the run’s progress')).toBeTruthy();
    await after(POLL_MS);

    expect(await screen.findByRole('region', { name: 'Headline figures' })).toBeTruthy();
    expect(cancelButton()).toBeNull();
    expect(updated.map((detail) => detail.status)).toEqual(['complete']);
    expect(jobs.refreshes).toBe(3);
  });

  it('follows the job once a later look finds it', async () => {
    const service = fakeService(vi);
    readsAs(service, going());
    const jobs = fakeJobs();
    const { view } = await renderRun(runItem(), { service, jobs });
    await screen.findByText('Waiting for the run’s progress');

    jobs.registerOnRefresh('job-7');
    await after(POLL_MS);
    await waitFor(() => expect(jobs.refreshes).toBe(2));
    jobs.emit('job-7', { type: 'job.phase', phase: 'replaying', friendly: 'Replaying each session' });

    expect(await screen.findByText('Replaying each session')).toBeTruthy();
    expect(screen.queryByText('Waiting for the run’s progress')).toBeNull();
    await after(2 * POLL_MS);
    expect(jobs.refreshes).toBe(2);
    view.fixture.destroy();
  });

  it('ends on what the record says when a cancel was accepted and the job never ends', async () => {
    const service = fakeService(vi);
    readsAs(service, going(), going(), without({ status: 'interrupted' }));
    const jobs = fakeJobs();
    jobs.register('job-7');
    const { updated } = await renderRun(runItem(), { service, jobs });
    jobs.emit('job-7', { type: 'job.phase', phase: 'replaying', friendly: 'Replaying each session' });
    screen.getByRole('button', { name: 'Cancel' }).click();
    await screen.findByText('Waiting for the run to stop. Nothing will be saved.');

    await after(POLL_MS);
    await after(POLL_MS);

    expect(await screen.findByText('This run did not finish, and its job is gone. It saved no result.')).toBeTruthy();
    expect(cancelButton()).toBeNull();
    expect(screen.queryByText('Waiting for the run to stop. Nothing will be saved.')).toBeNull();
    expect(updated.map((detail) => detail.status)).toEqual(['interrupted']);
  });
});

describe('when a read of the record fails while the run is going', () => {
  it('keeps reading, says once in plain words that the record could not be read, and says no more when it can', async () => {
    const service = fakeService(vi);
    service.run
      .mockResolvedValueOnce(going())
      .mockRejectedValueOnce(new Error('offline'))
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValue(runDetail());
    await renderRun(runItem(), { service });
    await screen.findByRole('button', { name: 'Cancel' });
    expect(screen.queryByText(/could not be read/)).toBeNull();

    await after(POLL_MS);
    const said = await screen.findByText('The run could not be read. The page is still looking.');
    expect(said.getAttribute('role')).toBe('status');
    expect(cancelButton()).not.toBeNull();
    await after(POLL_MS);
    await waitFor(() => expect(service.run).toHaveBeenCalledTimes(3));

    expect(screen.getAllByText(/could not be read/)).toHaveLength(1);
    expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull();
    await after(POLL_MS);
    expect(await screen.findByRole('region', { name: 'Headline figures' })).toBeTruthy();
    expect(screen.queryByText(/could not be read/)).toBeNull();
    expect(within(screen.getByRole('region', { name: 'Backtest run' })).queryByRole('alert')).toBeNull();
  });
});

/** Lets the page draw what a read just left, without moving the poll's clock far. */
async function lettingGoOnce(): Promise<void> {
  await after(50);
}
