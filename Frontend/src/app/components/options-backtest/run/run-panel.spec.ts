import { render, screen, waitFor, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { JobsService } from '../../../services/jobs.service';
import { OptionsBacktestService, RunRefusedError } from '../options-backtest.service';
import type { RunDetail, RunListItem, RunStatus } from '../options-backtest.types';
import { fakeJobs, type FakeJobs } from '../testing/fake-jobs';
import { expectTradingDatesInNewYork, lettingGo, reads } from '../testing/page-actions';
import { fakeService, type FakeService } from '../testing/render-page';
import { runDetail, runItem, runWithoutSetup, SETUP_CANNOT_OPEN_REASON } from '../testing/run-fixtures';
import { RunPanelComponent } from './run-panel.component';

async function renderRun(run: RunListItem, options: { service?: FakeService; jobs?: FakeJobs; showSetup?: boolean } = {}) {
  const service = options.service ?? fakeService(vi);
  const jobs = options.jobs ?? fakeJobs();
  const view = await render(RunPanelComponent, {
    inputs: { run, showSetup: options.showSetup ?? false },
    providers: [
      { provide: OptionsBacktestService, useValue: service },
      { provide: JobsService, useValue: jobs.service },
    ],
  });
  const updated: RunDetail[] = [];
  view.fixture.componentInstance.updated.subscribe((detail) => updated.push(detail));
  return { view, service, jobs, updated };
}

/** The record the run is read as, once for each status it passes through. */
function readsAs(service: FakeService, ...records: RunDetail[]): void {
  let next = 0;
  service.run.mockImplementation(async () => records[Math.min(next++, records.length - 1)] ?? runDetail());
}

const going = (): RunDetail => runDetail({ status: 'running', finished_at_ms: null, figures: null, slippage: [], slippage_tolerance: null, headline: null });

describe('a run that is going', () => {
  it('says the phase in the job’s own words, counts sessions only in the phases that count them, and shows the latest log line', async () => {
    const service = fakeService(vi);
    readsAs(service, going());
    const jobs = fakeJobs();
    jobs.register('job-7');
    await renderRun(runItem(), { service, jobs });

    jobs.emit('job-7', { type: 'job.started' });
    jobs.emit('job-7', { type: 'job.phase', phase: 'checking_data', friendly: 'Checking which sessions can be tested' });
    expect(await screen.findByText('Checking which sessions can be tested')).toBeTruthy();
    expect(screen.queryByText(/of \d+ sessions/)).toBeNull();

    jobs.emit('job-7', { type: 'job.phase', phase: 'downloading_data', friendly: 'Fetching the market data the run is missing' });
    jobs.emit('job-7', { type: 'job.progress', current: 2, total: 5, unit: 'sessions' });
    jobs.emit('job-7', { type: 'job.log', message: 'Fetched the listing for session 2 of 5.' });
    expect(await screen.findByText('Fetching the market data the run is missing')).toBeTruthy();
    expect(screen.getByText('2 of 5 sessions')).toBeTruthy();
    expect(screen.getByText('Fetched the listing for session 2 of 5.')).toBeTruthy();
    const bar = screen.getByLabelText('Sessions done') as HTMLProgressElement;
    expect([bar.value, bar.max]).toEqual([2, 5]);

    jobs.emit('job-7', { type: 'job.phase', phase: 'waiting_for_engine', friendly: 'Waiting for the backtest already running' });
    expect(await screen.findByText('Waiting for the backtest already running')).toBeTruthy();
    expect(screen.queryByText(/of \d+ sessions/)).toBeNull();

    jobs.emit('job-7', { type: 'job.phase', phase: 'replaying', friendly: 'Replaying each session' });
    jobs.emit('job-7', { type: 'job.progress', current: 1, total: 5, unit: 'sessions' });
    expect(await screen.findByText('Replaying each session')).toBeTruthy();
    expect(screen.getByText('1 of 5 sessions')).toBeTruthy();

    jobs.emit('job-7', { type: 'job.phase', phase: 'saving', friendly: 'Saving the run' });
    expect(await screen.findByText('Saving the run')).toBeTruthy();
    expect(screen.queryByText(/of \d+ sessions/)).toBeNull();
  });

  it('shows the server’s two lines, one when a download starts to wait for another request and one when it ends, while the count does not move', async () => {
    const service = fakeService(vi);
    readsAs(service, going());
    const jobs = fakeJobs();
    jobs.register('job-7');
    await renderRun(runItem(), { service, jobs });
    jobs.emit('job-7', { type: 'job.phase', phase: 'downloading_data', friendly: 'Fetching the market data the run is missing' });
    jobs.emit('job-7', { type: 'job.progress', current: 2, total: 5, unit: 'sessions' });
    jobs.emit('job-7', { type: 'job.log', message: 'Fetched the listing for session 2 of 5.' });
    await screen.findByText('Fetched the listing for session 2 of 5.');

    jobs.emit('job-7', { type: 'job.log', message: 'Waiting for data another request is already fetching.' });

    expect(await screen.findByText('Waiting for data another request is already fetching.')).toBeTruthy();
    expect(screen.queryByText('Fetched the listing for session 2 of 5.')).toBeNull();
    expect(screen.getByText('2 of 5 sessions')).toBeTruthy();
    jobs.emit('job-7', { type: 'job.log', message: 'The other request has finished; carrying on.' });
    expect(await screen.findByText('The other request has finished; carrying on.')).toBeTruthy();
    expect(screen.queryByText('Waiting for data another request is already fetching.')).toBeNull();
    expect(screen.getByText('2 of 5 sessions')).toBeTruthy();
  });

  it('announces the phase to a screen reader and not the count', async () => {
    const service = fakeService(vi);
    readsAs(service, going());
    const jobs = fakeJobs();
    jobs.register('job-7');
    await renderRun(runItem(), { service, jobs });
    jobs.emit('job-7', { type: 'job.phase', phase: 'replaying', friendly: 'Replaying each session' });
    jobs.emit('job-7', { type: 'job.progress', current: 1, total: 5, unit: 'sessions' });

    const phase = await screen.findByText('Replaying each session');

    expect(phase.getAttribute('role')).toBe('status');
    expect(screen.getByText('1 of 5 sessions').closest('[role="status"]')).toBeNull();
  });

  it('asks the server to stop when Cancel is pressed, once, and says it is waiting', async () => {
    const service = fakeService(vi);
    readsAs(service, going());
    const jobs = fakeJobs();
    jobs.register('job-7');
    await renderRun(runItem(), { service, jobs });
    jobs.emit('job-7', { type: 'job.phase', phase: 'replaying', friendly: 'Replaying each session' });

    screen.getByRole('button', { name: 'Cancel' }).click();

    await screen.findByText('Waiting for the run to stop. Nothing will be saved.');
    expect(jobs.cancelled).toEqual(['job-7']);
    const asked = screen.getByRole('button', { name: 'Cancel asked for' }) as HTMLButtonElement;
    expect(asked.disabled).toBe(true);
  });

  it('says the cancel could not be asked for, and lets it be asked again', async () => {
    const service = fakeService(vi);
    readsAs(service, going());
    const jobs = fakeJobs();
    jobs.register('job-7');
    jobs.failCancel = new Error('offline');
    await renderRun(runItem(), { service, jobs });

    screen.getByRole('button', { name: 'Cancel' }).click();

    expect((await screen.findByRole('alert')).textContent).toBe('The cancel could not be asked for. Try again.');
    expect((screen.getByRole('button', { name: 'Cancel' }) as HTMLButtonElement).disabled).toBe(false);
  });

  it('says the run was cancelled and that nothing was saved, once the job says it stopped', async () => {
    const service = fakeService(vi);
    readsAs(service, going(), runDetail({ status: 'cancelled', figures: null, slippage: [], slippage_tolerance: null, headline: null }));
    const jobs = fakeJobs();
    jobs.register('job-7');
    const { updated } = await renderRun(runItem(), { service, jobs });
    jobs.emit('job-7', { type: 'job.phase', phase: 'replaying', friendly: 'Replaying each session' });
    screen.getByRole('button', { name: 'Cancel' }).click();

    jobs.emit('job-7', { type: 'job.cancelled', reason: 'Cancelled by the owner.' });

    expect(await screen.findByText('The run was cancelled. Nothing was saved.')).toBeTruthy();
    expect(screen.queryByRole('button', { name: /Cancel/ })).toBeNull();
    expect(updated.map((detail) => detail.status)).toEqual(['cancelled']);
  });

  it('shows the result once the job completes, read from the run’s record and not from the job', async () => {
    const service = fakeService(vi);
    readsAs(service, going(), runDetail());
    const jobs = fakeJobs();
    jobs.register('job-7');
    const { updated } = await renderRun(runItem(), { service, jobs });
    jobs.emit('job-7', { type: 'job.phase', phase: 'saving', friendly: 'Saving the run' });

    jobs.emit('job-7', { type: 'job.completed', result_url: '/api/jobs/job-7/result' });

    expect(await screen.findByRole('region', { name: 'Headline figures' })).toBeTruthy();
    expect(service.run).toHaveBeenCalledTimes(2);
    expect(updated.map((detail) => detail.status)).toEqual(['complete']);
  });

  it('says the cancel came too late when the run finished and was saved all the same', async () => {
    const service = fakeService(vi);
    readsAs(service, going(), runDetail());
    const jobs = fakeJobs();
    jobs.register('job-7');
    await renderRun(runItem(), { service, jobs });
    jobs.emit('job-7', { type: 'job.phase', phase: 'saving', friendly: 'Saving the run' });
    screen.getByRole('button', { name: 'Cancel' }).click();
    await screen.findByRole('button', { name: 'Cancel asked for' });

    jobs.emit('job-7', { type: 'job.completed', result_url: '/x' });

    expect(await screen.findByText('You asked to cancel, but the run had already finished and was saved.')).toBeTruthy();
    expect(await screen.findByRole('region', { name: 'Headline figures' })).toBeTruthy();
  });

  it('does not say the cancel came too late for a run nobody asked to cancel', async () => {
    const service = fakeService(vi);
    readsAs(service, going(), runDetail());
    const jobs = fakeJobs();
    jobs.register('job-7');
    await renderRun(runItem(), { service, jobs });

    jobs.emit('job-7', { type: 'job.completed', result_url: '/x' });

    await screen.findByRole('region', { name: 'Headline figures' });
    expect(screen.queryByText(/You asked to cancel/)).toBeNull();
  });

  it('shows why a run failed and each thing that failed, when the job fails', async () => {
    const failure = {
      code: 'DOWNLOAD_FAILED',
      reason: 'The market data for some sessions could not be fetched.',
      items: [
        { session_open_ms: runItem().first_session_open_ms, code: 'listing', message: 'The vendor refused the listing.' },
        { session_open_ms: null, code: 'internal', message: 'The run stopped early.' },
      ],
    };
    const service = fakeService(vi);
    readsAs(service, going(), runDetail({ status: 'failed', failure, figures: null, slippage: [], slippage_tolerance: null, headline: null }));
    const jobs = fakeJobs();
    jobs.register('job-7');
    await renderRun(runItem(), { service, jobs });

    jobs.emit('job-7', { type: 'job.failed', code: 'RunFailedError', message: failure.reason });

    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText('The market data for some sessions could not be fetched.')).toBeTruthy();
    expect(within(alert).getByText('Download Failed')).toBeTruthy();
    const items = within(alert).getAllByRole('listitem').map((item) => reads(item));
    expect(items).toEqual(['2026-09-14 – The vendor refused the listing.', 'The run as a whole – The run stopped early.']);
    expectTradingDatesInNewYork(alert);
  });

  it('finds a job that this tab was not told of, and follows it', async () => {
    const service = fakeService(vi);
    readsAs(service, going());
    const jobs = fakeJobs();
    jobs.registerOnRefresh('job-7');

    await renderRun(runItem(), { service, jobs });

    await waitFor(() => expect(jobs.refreshes).toBe(1));
    jobs.emit('job-7', { type: 'job.phase', phase: 'replaying', friendly: 'Replaying each session' });
    expect(await screen.findByText('Replaying each session')).toBeTruthy();
    expect(service.run).toHaveBeenCalledTimes(1);
  });

  it('reads the run again when its job cannot be found, and shows what became of it', async () => {
    const service = fakeService(vi);
    readsAs(service, going(), runDetail({ status: 'interrupted', figures: null, slippage: [], slippage_tolerance: null, headline: null }));
    const jobs = fakeJobs();

    await renderRun(runItem(), { service, jobs });

    expect(await screen.findByText('This run did not finish, and its job is gone. It saved no result.')).toBeTruthy();
    expect(jobs.refreshes).toBe(1);
  });

  it('keeps the later reading of a run when an earlier one comes back after it', async () => {
    const service = fakeService(vi);
    const answers: ((detail: RunDetail) => void)[] = [];
    service.run.mockImplementation(() => new Promise<RunDetail>((resolve) => answers.push(resolve)));
    const jobs = fakeJobs();
    jobs.register('job-7');
    await renderRun(runItem(), { service, jobs });
    await waitFor(() => expect(answers).toHaveLength(1));
    jobs.emit('job-7', { type: 'job.completed', result_url: '/x' });
    await waitFor(() => expect(answers).toHaveLength(2));

    answers[1]?.(runDetail());
    expect(await screen.findByRole('region', { name: 'Headline figures' })).toBeTruthy();
    answers[0]?.(going());
    await lettingGo();

    expect(screen.getByRole('region', { name: 'Headline figures' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Cancel' })).toBeNull();
  });

  it('reads a run that is over once, even when its job is still known to this tab and ended', async () => {
    const service = fakeService(vi);
    readsAs(service, runDetail());
    const jobs = fakeJobs();
    jobs.register('job-7');
    jobs.emit('job-7', { type: 'job.completed', result_url: '/x' });

    await renderRun(runItem({ status: 'complete' }), { service, jobs });

    await screen.findByRole('region', { name: 'Headline figures' });
    await lettingGo();
    expect(service.run).toHaveBeenCalledTimes(1);
  });

  it('does not ask for the jobs again for a run that is not going', async () => {
    const service = fakeService(vi);
    readsAs(service, runDetail());
    const jobs = fakeJobs();

    await renderRun(runItem({ status: 'complete' }), { service, jobs });

    await screen.findByRole('region', { name: 'Headline figures' });
    expect(jobs.refreshes).toBe(0);
    expect(service.run).toHaveBeenCalledTimes(1);
  });

  it('reads the run again once when its job had already ended before the page came back', async () => {
    const service = fakeService(vi);
    readsAs(service, going(), runDetail());
    const jobs = fakeJobs();
    jobs.register('job-7');
    jobs.emit('job-7', { type: 'job.completed', result_url: '/x' });

    await renderRun(runItem(), { service, jobs });

    expect(await screen.findByRole('region', { name: 'Headline figures' })).toBeTruthy();
    expect(service.run).toHaveBeenCalledTimes(2);
  });
});

describe('a run that is over, opened later', () => {
  it.each<[RunStatus, string]>([
    ['cancelled', 'The run was cancelled. Nothing was saved.'],
    ['interrupted', 'This run did not finish, and its job is gone. It saved no result.'],
  ])('says what became of a run that is %s', async (status, words) => {
    const service = fakeService(vi);
    readsAs(service, runDetail({ status, figures: null, slippage: [], slippage_tolerance: null, headline: null }));

    await renderRun(runItem({ status }), { service });

    expect(await screen.findByText(words)).toBeTruthy();
  });

  it('says what the run was: the underlying, the dates in New York dates, the sessions asked for, and the name it ran under', async () => {
    const service = fakeService(vi);
    readsAs(service, runDetail({ setup_name: 'SPY premium momentum' }));

    await renderRun(runItem({ status: 'complete', setup_name: 'SPY premium momentum' }), { service });

    const about = (await screen.findByText(/sessions requested/)).closest('ul');
    const items = [...(about?.querySelectorAll('li') ?? [])].map((item) => reads(item));
    // The start is an instant, shown in the viewer's own zone; its date is the 21st in any zone from New York to Chicago.
    expect(items.slice(0, 4)).toEqual(['SPY', 'SPY premium momentum', '2026-09-14 – 2026-09-18', '5 sessions requested']);
    expectTradingDatesInNewYork(about as HTMLElement);
    expect(items[4]).toMatch(/^Started 2026-09-2[12] \d\d:\d\d:\d\d$/);
  });

  it('says plainly when the run cannot be read, and reads it again on request', async () => {
    const service = fakeService(vi);
    service.run.mockRejectedValueOnce(new RunRefusedError('OPTIONS_BACKTEST_RUN_NOT_FOUND', 'No run 7.', 404));
    service.run.mockResolvedValue(runDetail());
    await renderRun(runItem({ status: 'complete' }), { service });

    expect((await screen.findByText('No run 7.')).getAttribute('role')).toBe('alert');
    screen.getByRole('button', { name: 'Try again' }).click();

    expect(await screen.findByRole('region', { name: 'Headline figures' })).toBeTruthy();
  });

  it('shows the setup the run used in the server’s sentences, and gives the setup back when asked to use it', async () => {
    const service = fakeService(vi);
    const detail = runDetail();
    readsAs(service, detail);
    const { view } = await renderRun(runItem({ status: 'complete' }), { service, showSetup: true });
    const used: unknown[] = [];
    view.fixture.componentInstance.useSetup.subscribe((setup) => used.push(setup));

    await screen.findByText('Window for SPY.');
    screen.getByRole('button', { name: 'Use this setup' }).click();

    expect(service.describe).toHaveBeenCalledWith(detail.setup);
    expect(used).toEqual([detail.setup]);
  });

  it('can still be used when the setup cannot be put into words', async () => {
    const service = fakeService(vi, {
      describe: vi.fn(async () => {
        throw new Error('offline');
      }),
    });
    const detail = runDetail();
    readsAs(service, detail);
    const { view } = await renderRun(runItem({ status: 'complete' }), { service, showSetup: true });
    const used: unknown[] = [];
    view.fixture.componentInstance.useSetup.subscribe((setup) => used.push(setup));

    expect(await screen.findByText('The setup of this run could not be put into words. You can still use it.')).toBeTruthy();
    screen.getByRole('button', { name: 'Use this setup' }).click();

    expect(used).toEqual([detail.setup]);
  });

  it('does not ask for the setup’s sentences, nor offer it, unless it is asked to', async () => {
    const service = fakeService(vi);
    readsAs(service, runDetail());

    await renderRun(runItem({ status: 'complete' }), { service });

    await screen.findByRole('region', { name: 'Headline figures' });
    expect(service.describe).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: 'Use this setup' })).toBeNull();
  });
});

describe('a run whose setup the server cannot open', () => {
  it('shows the figures and no banner, says why in the server’s words, asks nothing to describe a setup that is not there, and cannot be used', async () => {
    const service = fakeService(vi);
    readsAs(service, runWithoutSetup());
    const { view } = await renderRun(runItem({ status: 'complete' }), { service, showSetup: true });
    const used: unknown[] = [];
    view.fixture.componentInstance.useSetup.subscribe((setup) => used.push(setup));

    const section = await screen.findByRole('region', { name: 'The setup this run used' });
    expect(await within(section).findByText(SETUP_CANNOT_OPEN_REASON)).toBeTruthy();
    expect(screen.getByRole('region', { name: 'Headline figures' })).toBeTruthy();
    expect(screen.queryByRole('note', { name: 'How these figures were made' })).toBeNull();
    const use = within(section).getByRole<HTMLButtonElement>('button', { name: 'Use this setup' });
    expect(use.disabled).toBe(true);
    use.click();
    await lettingGo();
    expect(service.describe).not.toHaveBeenCalled();
    expect(used).toEqual([]);
  });

  it('says nothing about a setup, and offers none, until the run has been read', async () => {
    const service = fakeService(vi);
    service.run.mockImplementation(() => new Promise<RunDetail>(() => undefined));

    await renderRun(runItem({ status: 'complete' }), { service, showSetup: true });

    const section = screen.getByRole('region', { name: 'The setup this run used' });
    expect(within(section).getByRole<HTMLButtonElement>('button', { name: 'Use this setup' }).disabled).toBe(true);
    expect(within(section).queryByText(SETUP_CANNOT_OPEN_REASON)).toBeNull();
  });
});
