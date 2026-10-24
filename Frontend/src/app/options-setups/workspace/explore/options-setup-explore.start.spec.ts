import { fireEvent, screen, waitFor, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { RunRefusedError } from '../../../components/options-backtest/options-backtest.service';
import type { RunStartRequest, SavedSetup } from '../../../components/options-backtest/options-backtest.types';
import { fakeJobs, type FakeJobs } from '../../../components/options-backtest/testing/fake-jobs';
import { fakeService, type FakeService } from '../../../components/options-backtest/testing/fake-service';
import { EXAMPLE_ONE_REENTRY, exampleBody, servedExamples, servedSaved } from '../../../components/options-backtest/testing/fixtures';
import { dateFields, pickDates, reads } from '../../../components/options-backtest/testing/page-actions';
import { runDetail, runItem } from '../../../components/options-backtest/testing/run-fixtures';
import { goingDetail as going, scriptedRun } from '../../../components/options-backtest/testing/run-script';
import { etDayEndMs, etMidnightMs } from '../../../shared/date/et-midnight';
import { renderExplore } from '../../testing/render-workspace';

const startButton = () => screen.getByRole<HTMLButtonElement>('button', { name: 'Start backtest' });
const shiftEnter = (target: EventTarget = document.body, init: KeyboardEventInit = {}) =>
  target.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', shiftKey: true, bubbles: true, cancelable: true, ...init }));

async function readyToStart(setup: { service: FakeService; jobs: FakeJobs } = scriptedRun(vi), dates: [string, string] = ['2026-09-14', '2026-09-18']) {
  await renderExplore(setup.service, EXAMPLE_ONE_REENTRY, { jobs: setup.jobs });
  pickDates(...dates);
  await waitFor(() => expect(startButton().disabled).toBe(false));
  return setup;
}

describe('when a backtest can be started', () => {
  it('needs the saved setup read back by the server, and two dates, and says which is missing', async () => {
    const service = fakeService(vi);
    await renderExplore(service, EXAMPLE_ONE_REENTRY);

    await waitFor(() => expect(screen.getByText('Check data and Start are available once both dates are picked.')).toBeTruthy());
    expect(startButton().disabled).toBe(true);
    pickDates('2026-09-14', '2026-09-18');
    await waitFor(() => expect(startButton().disabled).toBe(false));
    expect(screen.queryByText(/available once/)).toBeNull();
  });

  it('waits for the server to read the saved setup back, and says so', async () => {
    const service = fakeService(vi, { describe: vi.fn(() => new Promise<never>(() => undefined)) });
    await renderExplore(service, EXAMPLE_ONE_REENTRY, { settled: false });
    await screen.findByRole('heading', { name: 'Duration' });
    pickDates('2026-09-14', '2026-09-18');

    expect(await screen.findByText('Check data and Start are available once the server has read the saved setup back.')).toBeTruthy();
    expect(startButton().disabled).toBe(true);
    expect(startButton().getAttribute('aria-describedby')).toBe('ob-run-hint');
  });

  it('shows its shortcut on the button, and tells assistive technology about it', async () => {
    await renderExplore(fakeService(vi), EXAMPLE_ONE_REENTRY);

    expect(startButton().textContent).toContain('Shift+Enter');
    expect(startButton().getAttribute('aria-keyshortcuts')).toBe('Shift+Enter');
    expect(startButton().querySelector('kbd')?.getAttribute('aria-hidden')).toBe('true');
  });
});

describe('starting a backtest', () => {
  it('starts the job with the saved setup as the server read it, the two instants, and the name it is saved under, with the dates in no part of the setup, asks for the run it made by its job id, and shows it going', async () => {
    const setup = await readyToStart();

    fireEvent.click(startButton());

    await waitFor(() => expect(setup.service.startRun).toHaveBeenCalledTimes(1));
    const request = setup.service.startRun.mock.calls[0]?.[0];
    const answer = await setup.service.describe.mock.results.at(-1)?.value;
    expect.soft(request, 'starts the job with the setup as the server read it, the two instants, and the saved name').toEqual({ setup: answer.setup, start_ms: etMidnightMs('2026-09-14'), end_ms: etDayEndMs('2026-09-18'), setup_name: EXAMPLE_ONE_REENTRY });
    expect.soft(JSON.stringify(request?.setup), 'puts the dates in no part of the setup it sends').not.toMatch(/2026-09|start_ms|end_ms/);
    const panel = await screen.findByRole('region', { name: 'Backtest run' });
    expect.soft(setup.service.runs, 'asks for the run it made by its job id').toHaveBeenCalledWith({ jobId: 'job-1' });
    expect.soft(within(panel).getByText('Running'), 'shows the run going').toBeTruthy();
    expect.soft(within(panel).getByRole('button', { name: 'Cancel' }), 'offers Cancel on the run going').toBeTruthy();
    await waitFor(() => expect(document.activeElement, 'moves the focus to the heading of the run').toBe(within(panel).getByRole('heading', { name: 'Backtest run' })));
  });
});

describe('the name a run is recorded under', () => {
  const library = (...extra: SavedSetup[]) => [...servedExamples(), ...extra];
  const sent = (setup: { service: FakeService }): RunStartRequest | undefined => setup.service.startRun.mock.calls[0]?.[0];

  it('is the saved name of the setup, as the address says', async () => {
    const setup = scriptedRun(vi, { list: vi.fn(async () => library(servedSaved('Mine', exampleBody()))) });
    await renderExplore(setup.service, 'Mine', { jobs: setup.jobs });
    pickDates('2026-09-14', '2026-09-18');
    await waitFor(() => expect(startButton().disabled).toBe(false));

    fireEvent.click(startButton());

    await waitFor(() => expect(sent(setup)?.setup_name).toBe('Mine'));
  });

  it('is the name of the setup whose Explore it is, never another, when the address is changed to another setup', async () => {
    const other = { ...exampleBody(), slippage_percent: '0.45' };
    const setup = scriptedRun(vi, { list: vi.fn(async () => library(servedSaved('Mine', exampleBody()), servedSaved('Other', other))) });
    await renderExplore(setup.service, 'Other', { jobs: setup.jobs });
    pickDates('2026-09-14', '2026-09-18');
    await waitFor(() => expect(startButton().disabled).toBe(false));

    fireEvent.click(startButton());

    await waitFor(() => expect(sent(setup)?.setup_name).toBe('Other'));
    expect(sent(setup)?.setup).toMatchObject({ slippage_percent: '0.45' });
  });
});

describe('the shortcut, Shift+Enter', () => {
  it('starts a backtest from anywhere on the page, once', async () => {
    const setup = await readyToStart();

    shiftEnter();
    shiftEnter();

    await screen.findByRole('region', { name: 'Backtest run' });
    expect(setup.service.startRun).toHaveBeenCalledTimes(1);
  });

  it('takes the key when it starts a backtest, so that a button that has the focus is not pressed as well', async () => {
    await readyToStart();
    const event = new KeyboardEvent('keydown', { key: 'Enter', shiftKey: true, bubbles: true, cancelable: true });

    document.body.dispatchEvent(event);

    expect(event.defaultPrevented).toBe(true);
    await screen.findByRole('region', { name: 'Backtest run' });
  });

  it('does nothing while the backtest cannot be started', async () => {
    const service = fakeService(vi);
    await renderExplore(service, EXAMPLE_ONE_REENTRY);
    const event = new KeyboardEvent('keydown', { key: 'Enter', shiftKey: true, bubbles: true, cancelable: true });

    document.body.dispatchEvent(event);

    expect(service.startRun).not.toHaveBeenCalled();
    expect(event.defaultPrevented).toBe(false);
  });

  it('does nothing in a field that uses Shift+Enter for a new line, and does not take the key from it', async () => {
    const setup = await readyToStart();
    const notes = document.body.appendChild(document.createElement('textarea'));
    notes.focus();
    const event = new KeyboardEvent('keydown', { key: 'Enter', shiftKey: true, bubbles: true, cancelable: true });

    notes.dispatchEvent(event);

    expect(setup.service.startRun).not.toHaveBeenCalled();
    expect(event.defaultPrevented).toBe(false);
    notes.remove();
  });

  it('does nothing for a key that is held down, or typed through an input method, or without Shift, or with another key held as well', async () => {
    const setup = await readyToStart();

    shiftEnter(document.body, { repeat: true });
    shiftEnter(document.body, { isComposing: true });
    document.body.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
    shiftEnter(document.body, { ctrlKey: true });

    expect(setup.service.startRun).not.toHaveBeenCalled();
  });

  it('starts with the date that was typed and has not yet been applied', async () => {
    const setup = await readyToStart();
    const start = screen.getByLabelText<HTMLInputElement>('Start date');
    // A browser applies a typed date when its field is left; the page leaves the field before it starts.
    start.addEventListener('blur', () => start.dispatchEvent(new Event('change', { bubbles: true })));
    start.focus();
    start.value = '2026-09-15';

    shiftEnter(start);

    await waitFor(() => expect(setup.service.startRun).toHaveBeenCalledTimes(1));
    expect(setup.service.startRun.mock.calls[0]?.[0].start_ms).toBe(etMidnightMs('2026-09-15'));
    expect(dateFields().from).toBe('2026-09-15');
  });

  it('starts nothing while a backtest is going', async () => {
    const setup = await readyToStart();
    fireEvent.click(startButton());
    await screen.findByRole('region', { name: 'Backtest run' });

    shiftEnter();

    expect(setup.service.startRun).toHaveBeenCalledTimes(1);
  });
});

describe('when the server will not start the backtest', () => {
  it('shows a range it refuses at the dates, in its own words, starts no run, and forgets the refusal when the dates change', async () => {
    const setup = scriptedRun(vi, {
      startRun: vi.fn(async () => {
        throw new RunRefusedError('OPTIONS_BACKTEST_RANGE_HAS_NO_SESSION', 'No trading session falls in that range.', 400);
      }),
    });
    await readyToStart(setup);

    fireEvent.click(startButton());

    const reason = await within(screen.getByRole('region', { name: 'Duration' })).findByText('No trading session falls in that range.');
    expect(reason.getAttribute('role')).toBe('alert');
    expect(screen.queryByRole('region', { name: 'Backtest run' })).toBeNull();
    expect(startButton().disabled).toBe(false);
    pickDates('2026-09-15', '2026-09-18');
    await waitFor(() => expect(screen.queryByText('No trading session falls in that range.')).toBeNull());
  });

  it('shows a refusal that is not about the range in the server’s words with its code, says nothing of trying again, and leaves Start free', async () => {
    const setup = scriptedRun(vi, {
      startRun: vi.fn(async () => {
        throw new RunRefusedError('OPTIONS_BACKTEST_JOB_ALREADY_HAS_A_RUN', 'A run was already made for this job.', 409);
      }),
    });
    await readyToStart(setup);

    fireEvent.click(startButton());

    const notice = (await screen.findByText('A run was already made for this job.')).closest('[role="alert"]') as HTMLElement;
    expect(reads(notice)).toBe('A run was already made for this job. Options Backtest Job Already Has A Run');
    expect(within(notice).queryByRole('button', { name: 'Try again' })).toBeNull();
    expect(screen.queryByText(/Try again/)).toBeNull();
    expect(startButton().disabled).toBe(false);
  });
});

describe('coming back to the page while a backtest is going', () => {
  it('shows the run that is going, with its progress, and does not leave Start free to begin another', async () => {
    const jobs = fakeJobs();
    jobs.register('job-9');
    jobs.emit('job-9', { type: 'job.phase', phase: 'replaying', friendly: 'Replaying each session' });
    jobs.emit('job-9', { type: 'job.progress', current: 4, total: 5, unit: 'sessions' });
    const service = fakeService(vi, {
      runs: vi.fn(async () => [runItem({ id: 4, status: 'complete', job_id: 'job-4' }), runItem({ id: 9, job_id: 'job-9' })]),
      run: vi.fn(async () => going()),
    });

    await renderExplore(service, EXAMPLE_ONE_REENTRY, { jobs });

    const panel = await screen.findByRole('region', { name: 'Backtest run' });
    expect(within(panel).getByText('Replaying each session')).toBeTruthy();
    expect(within(panel).getByText('Sessions done: 4 of 5')).toBeTruthy();
    pickDates('2026-09-14', '2026-09-18');
    await waitFor(() => expect(screen.getByText('A backtest is already running.')).toBeTruthy());
    expect(startButton().disabled).toBe(true);
    shiftEnter();
    expect(service.startRun).not.toHaveBeenCalled();
  });

  it('keeps the backtest on screen when the owner goes to Define and comes back', async () => {
    const setup = scriptedRun(vi);
    const view = await renderExplore(setup.service, EXAMPLE_ONE_REENTRY, { jobs: setup.jobs });
    pickDates('2026-09-14', '2026-09-18');
    await waitFor(() => expect(startButton().disabled).toBe(false));
    fireEvent.click(startButton());
    await screen.findByRole('region', { name: 'Backtest run' });

    fireEvent.click(within(screen.getByRole('navigation', { name: 'Options setup tabs' })).getByRole('link', { name: 'Define' }));
    await screen.findByRole('heading', { name: 'Entry settings' });
    expect(screen.queryByRole('region', { name: 'Backtest run' })).toBeNull();
    fireEvent.click(within(screen.getByRole('navigation', { name: 'Options setup tabs' })).getByRole('link', { name: 'Explore' }));
    await screen.findByRole('heading', { name: 'Duration' });
    await view.fixture.whenStable();

    expect(screen.getByRole('region', { name: 'Backtest run' })).toBeTruthy();
    expect(dateFields()).toEqual({ from: '2026-09-14', to: '2026-09-18' });
    expect(startButton().disabled).toBe(true);
    expect(setup.service.startRun).toHaveBeenCalledTimes(1);
  });
});

describe('when the page cannot tell whether a backtest is going', () => {
  it('offers to ask again, and shows the backtest that is going once the history answers', async () => {
    const jobs = fakeJobs();
    jobs.register('job-9');
    // Each answer is made when it is asked for: a rejection made earlier would be unhandled until the page asks.
    const answers = [() => Promise.reject(new Error('offline')), () => Promise.resolve([runItem({ id: 9, job_id: 'job-9' })])];
    const service = fakeService(vi, { runs: vi.fn(() => answers.shift()?.() ?? Promise.resolve([])), run: vi.fn(async () => going()) });
    await renderExplore(service, EXAMPLE_ONE_REENTRY, { jobs });
    const notice = (await screen.findByText(/Whether a backtest is already running could not be checked/)).closest('[role="alert"]') as HTMLElement;

    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));

    expect(await screen.findByRole('region', { name: 'Backtest run' })).toBeTruthy();
    expect(screen.queryByText(/could not be checked/)).toBeNull();
    expect(notice.isConnected).toBe(false);
  });
});

describe('a backtest from start to end', () => {
  it('shows the result when it completes, and lets another be started', async () => {
    const setup = scriptedRun(vi);
    setup.service.run.mockResolvedValueOnce(going()).mockResolvedValue(runDetail());
    await readyToStart(setup);
    fireEvent.click(startButton());
    await screen.findByRole('region', { name: 'Backtest run' });
    setup.jobs.emit('job-1', { type: 'job.phase', phase: 'replaying', friendly: 'Replaying each session' });
    expect(startButton().disabled).toBe(true);

    setup.jobs.emit('job-1', { type: 'job.completed', result_url: '/api/jobs/job-1/result' });

    expect(await screen.findByRole('region', { name: 'Headline figures' })).toBeTruthy();
    await waitFor(() => expect(startButton().disabled).toBe(false));
    expect(screen.queryByText('A backtest is already running.')).toBeNull();
    // Read when it began and once more when it ended: the page handing the newer record back does not read it a third time.
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(setup.service.run).toHaveBeenCalledTimes(2);
  });

  it('offers Cancel afresh for the next backtest, in the same place, after one that was cancelled', async () => {
    const setup = scriptedRun(vi);
    const ids = [1, 2];
    setup.service.startRun.mockImplementation(async () => {
      const jobId = `job-${ids[0]}`;
      setup.jobs.register(jobId);
      return jobId;
    });
    setup.service.runs.mockImplementation(async (query) => (query?.jobId === undefined ? [] : [runItem({ id: ids[0], job_id: query.jobId })]));
    setup.service.run.mockImplementation(async (id) => (id === 1 ? goingThenCancelled.shift() ?? going() : going()));
    const goingThenCancelled = [going(), runDetail({ id: 1, job_id: 'job-1', status: 'cancelled', figures: null, slippage: [], slippage_tolerance: null, headline: null })];
    await readyToStart(setup);
    fireEvent.click(startButton());
    const first = await screen.findByRole('region', { name: 'Backtest run' });
    within(first).getByRole('button', { name: 'Cancel' }).click();
    await within(first).findByRole('button', { name: 'Cancel asked for' });
    setup.jobs.emit('job-1', { type: 'job.cancelled', reason: 'Cancelled.' });
    await within(first).findByText('The run was cancelled. Nothing was saved.');
    await waitFor(() => expect(startButton().disabled).toBe(false));
    ids.shift();

    fireEvent.click(startButton());

    await waitFor(() => expect(setup.service.startRun).toHaveBeenCalledTimes(2));
    const second = await screen.findByRole('region', { name: 'Backtest run' });
    const cancel = await within(second).findByRole('button', { name: 'Cancel' });
    expect((cancel as HTMLButtonElement).disabled).toBe(false);
  });

  it('keeps Check data available while a backtest is going', async () => {
    await readyToStart();
    fireEvent.click(startButton());
    await screen.findByRole('region', { name: 'Backtest run' });

    expect(screen.getByRole<HTMLButtonElement>('button', { name: 'Check data' }).disabled).toBe(false);
  });
});
