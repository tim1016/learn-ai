import { fireEvent, screen, waitFor, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { etDayEndMs, etMidnightMs } from '../../shared/date/et-midnight';
import { RunRefusedError } from './options-backtest.service';
import type { RunStartRequest } from './options-backtest.types';
import { fakeJobs, type FakeJobs } from './testing/fake-jobs';
import { EXAMPLE_ONE_REENTRY, servedSaved, exampleBody } from './testing/fixtures';
import { addLeg, dateFields, lettingGo, openExample, pickDates, press, reads, typeInto } from './testing/page-actions';
import { fakeService, renderPage, type FakeService } from './testing/render-page';
import { runDetail, runItem } from './testing/run-fixtures';
import { goingDetail as going, scriptedRun } from './testing/run-script';

const startButton = () => screen.getByRole<HTMLButtonElement>('button', { name: 'Start backtest' });
const shiftEnter = (target: EventTarget = document.body, init: KeyboardEventInit = {}) =>
  target.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', shiftKey: true, bubbles: true, cancelable: true, ...init }));
async function readyToStart(setup: { service: FakeService; jobs: FakeJobs } = scriptedRun(vi), dates: [string, string] = ['2026-09-14', '2026-09-18']) {
  await renderPage(setup.service, undefined, { jobs: setup.jobs });
  await openExample();
  pickDates(...dates);
  await waitFor(() => expect(startButton().disabled).toBe(false));
  return setup;
}

describe('when a backtest can be started', () => {
  it('needs a setup the server has read back, and two dates, and says which is missing', async () => {
    await renderPage(fakeService(vi));

    expect(startButton().disabled).toBe(true);
    expect(screen.getByText('Check data and Start are available once the setup is complete.')).toBeTruthy();
    expect(startButton().getAttribute('aria-describedby')).toBe('ob-run-hint');
    await openExample();
    await waitFor(() => expect(screen.getByText('Check data and Start are available once both dates are picked.')).toBeTruthy());
    expect(startButton().disabled).toBe(true);
    pickDates('2026-09-14', '2026-09-18');
    await waitFor(() => expect(startButton().disabled).toBe(false));
    expect(screen.queryByText(/available once/)).toBeNull();
  });

  it('waits for a date the picker refuses, and does not make one up', async () => {
    await renderPage(fakeService(vi));
    await openExample();

    pickDates('2026-09-14', '2026-13-45');

    await waitFor(() => expect(screen.getByText('Check data and Start are available once both dates are picked.')).toBeTruthy());
    expect(startButton().disabled).toBe(true);
  });

  it('shows its shortcut on the button, and tells assistive technology about it', async () => {
    await renderPage(fakeService(vi));

    expect(startButton().textContent).toContain('Shift+Enter');
    expect(startButton().getAttribute('aria-keyshortcuts')).toBe('Shift+Enter');
    expect(startButton().querySelector('kbd')?.getAttribute('aria-hidden')).toBe('true');
  });
});

describe('starting a backtest', () => {
  it('starts the job with the setup as the server read it, the two instants, and the name of the example that was opened', async () => {
    const setup = await readyToStart();

    fireEvent.click(startButton());

    await waitFor(() => expect(setup.service.startRun).toHaveBeenCalledTimes(1));
    const request = setup.service.startRun.mock.calls[0]?.[0];
    const answer = await setup.service.describe.mock.results.at(-1)?.value;
    expect(request).toEqual({ setup: answer.setup, start_ms: etMidnightMs('2026-09-14'), end_ms: etDayEndMs('2026-09-18'), setup_name: EXAMPLE_ONE_REENTRY });
  });

  it('asks for the run it made by its job id, and shows it going', async () => {
    const setup = await readyToStart();

    fireEvent.click(startButton());

    const panel = await screen.findByRole('region', { name: 'Backtest run' });
    expect(setup.service.runs).toHaveBeenCalledWith({ jobId: 'job-1' });
    expect(within(panel).getByText('Running')).toBeTruthy();
    expect(within(panel).getByRole('button', { name: 'Cancel' })).toBeTruthy();
    await waitFor(() => expect(document.activeElement).toBe(within(panel).getByRole('heading', { name: 'Backtest run' })));
  });

  it('sends the dates of a range across the clock change in March, each end on its own side of it', async () => {
    const setup = await readyToStart(scriptedRun(vi), ['2026-03-06', '2026-03-09']);

    fireEvent.click(startButton());

    await waitFor(() => expect(setup.service.startRun).toHaveBeenCalledTimes(1));
    const request = setup.service.startRun.mock.calls[0]?.[0];
    expect([request?.start_ms, request?.end_ms]).toEqual([1_772_773_200_000, 1_773_115_200_000]);
  });

  it('puts the dates in no part of the setup it sends', async () => {
    const setup = await readyToStart();

    fireEvent.click(startButton());

    await waitFor(() => expect(setup.service.startRun).toHaveBeenCalledTimes(1));
    expect(JSON.stringify(setup.service.startRun.mock.calls[0]?.[0].setup)).not.toMatch(/2026-09|start_ms|end_ms/);
  });

  it('starts once, however many times it is pressed', async () => {
    const setup = await readyToStart();

    fireEvent.click(startButton());
    fireEvent.click(startButton());
    shiftEnter();
    await screen.findByRole('region', { name: 'Backtest run' });
    fireEvent.click(startButton());

    expect(setup.service.startRun).toHaveBeenCalledTimes(1);
    expect(startButton().disabled).toBe(true);
    expect(screen.getByText('A backtest is already running.')).toBeTruthy();
  });
});

describe('the name a run is recorded under', () => {
  const sent = (setup: { service: FakeService }): RunStartRequest | undefined => setup.service.startRun.mock.calls[0]?.[0];

  it('is the saved setup’s name while the setup on screen is the one that was opened', async () => {
    const setup = scriptedRun(vi, { list: vi.fn(async () => [servedSaved('Mine', exampleBody())]) });
    await renderPage(setup.service, undefined, { jobs: setup.jobs });
    fireEvent.click(await within(screen.getByRole('complementary', { name: 'Saved setups' })).findByRole('button', { name: /^Mine/ }));
    await screen.findByRole('article', { name: 'Leg 2' });
    pickDates('2026-09-14', '2026-09-18');
    await waitFor(() => expect(startButton().disabled).toBe(false));

    fireEvent.click(startButton());

    await waitFor(() => expect(sent(setup)?.setup_name).toBe('Mine'));
  });

  it('is nothing once the setup on screen has been edited, because the run is no longer that setup’s', async () => {
    const setup = scriptedRun(vi, { list: vi.fn(async () => [servedSaved('Mine', exampleBody())]) });
    await renderPage(setup.service, undefined, { jobs: setup.jobs });
    fireEvent.click(await within(screen.getByRole('complementary', { name: 'Saved setups' })).findByRole('button', { name: /^Mine/ }));
    await screen.findByRole('article', { name: 'Leg 2' });
    typeInto('Slippage (percent)', '0.5');
    pickDates('2026-09-14', '2026-09-18');
    await waitFor(() => expect(startButton().disabled).toBe(false));

    fireEvent.click(startButton());

    await waitFor(() => expect(setup.service.startRun).toHaveBeenCalledTimes(1));
    expect(sent(setup)?.setup_name).toBeNull();
  });

  it('is nothing for a setup built from nothing', async () => {
    const setup = scriptedRun(vi);
    await renderPage(setup.service, undefined, { jobs: setup.jobs });
    addLeg();
    pickDates('2026-09-14', '2026-09-18');
    await waitFor(() => expect(startButton().disabled).toBe(false));

    fireEvent.click(startButton());

    await waitFor(() => expect(setup.service.startRun).toHaveBeenCalledTimes(1));
    expect(sent(setup)?.setup_name).toBeNull();
  });

  it('is the name it was just saved under', async () => {
    const setup = scriptedRun(vi);
    await renderPage(setup.service, undefined, { jobs: setup.jobs });
    addLeg();
    pickDates('2026-09-14', '2026-09-18');
    typeInto('Setup name', 'Straddle');
    await waitFor(() => expect(screen.getByRole<HTMLButtonElement>('button', { name: 'Save setup' }).disabled).toBe(false));
    press('Save setup');
    await screen.findByText(/Saved “Straddle”/);

    fireEvent.click(startButton());

    await waitFor(() => expect(sent(setup)?.setup_name).toBe('Straddle'));
  });
});

describe('the name a run is recorded under, when a save is under way', () => {
  it('is the name of the setup opened meanwhile, not the one the save was of, when the save comes back after another setup was opened', async () => {
    let finish: () => void = () => undefined;
    const setup = scriptedRun(vi);
    setup.service.save.mockImplementation(
      (name: string, body) =>
        new Promise((resolve) => {
          finish = () => resolve({ name, example: false, saved_at_ms: 1_760_000_000_000, setup: body as never, cannot_open_reason: null });
        }),
    );
    await renderPage(setup.service, undefined, { jobs: setup.jobs });
    addLeg();
    typeInto('Setup name', 'Mine');
    await waitFor(() => expect(screen.getByRole<HTMLButtonElement>('button', { name: 'Save setup' }).disabled).toBe(false));
    press('Save setup');
    await waitFor(() => expect(setup.service.save).toHaveBeenCalledTimes(1));
    await openExample();
    pickDates('2026-09-14', '2026-09-18');

    finish();
    await lettingGo();
    await lettingGo();
    await waitFor(() => expect(startButton().disabled).toBe(false));
    fireEvent.click(startButton());

    await waitFor(() => expect(setup.service.startRun).toHaveBeenCalledTimes(1));
    expect(setup.service.startRun.mock.calls[0]?.[0].setup_name).toBe(EXAMPLE_ONE_REENTRY);
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
    await renderPage(service);
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

  it('shows a range that reaches before 1970 at the dates in the server’s words, starts no run, and leaves Start free', async () => {
    const setup = scriptedRun(vi, {
      startRun: vi.fn(async () => {
        throw new RunRefusedError('OPTIONS_BACKTEST_RANGE_TOO_EARLY', 'The range reaches a trading day before 1970, which a run cannot be asked for. Start the range later.', 400);
      }),
    });
    await readyToStart(setup);

    fireEvent.click(startButton());

    const reason = await within(screen.getByRole('region', { name: 'Duration' })).findByText('The range reaches a trading day before 1970, which a run cannot be asked for. Start the range later.');
    expect(reason.getAttribute('role')).toBe('alert');
    expect(screen.queryByRole('region', { name: 'Backtest run' })).toBeNull();
    expect(startButton().disabled).toBe(false);
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

  it('says plainly when the backtest could not be started for another reason, and lets it be started again', async () => {
    let down = true;
    const setup = scriptedRun(vi);
    setup.service.startRun.mockImplementation(async () => {
      if (down) throw new Error('the jobs service is down');
      setup.jobs.register('job-1');
      return 'job-1';
    });
    await readyToStart(setup);

    fireEvent.click(startButton());

    expect((await screen.findByText('The backtest could not be started. Try again.')).closest('[role="alert"]')).not.toBeNull();
    down = false;
    fireEvent.click(startButton());
    await screen.findByRole('region', { name: 'Backtest run' });
    expect(screen.queryByText('The backtest could not be started. Try again.')).toBeNull();
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

    await renderPage(service, undefined, { jobs });

    const panel = await screen.findByRole('region', { name: 'Backtest run' });
    expect(within(panel).getByText('Replaying each session')).toBeTruthy();
    expect(within(panel).getByText('4 of 5 sessions')).toBeTruthy();
    await openExample();
    pickDates('2026-09-14', '2026-09-18');
    await waitFor(() => expect(screen.getByText('A backtest is already running.')).toBeTruthy());
    expect(startButton().disabled).toBe(true);
    shiftEnter();
    expect(service.startRun).not.toHaveBeenCalled();
  });

  it('shows nothing, and leaves Start free, when no run is going', async () => {
    const service = fakeService(vi, { runs: vi.fn(async () => [runItem({ status: 'complete' })]) });
    await renderPage(service);

    expect(screen.queryByRole('region', { name: 'Backtest run' })).toBeNull();
  });

  it('says so when it cannot tell whether a run is going', async () => {
    const service = fakeService(vi, {
      runs: vi.fn(async () => {
        throw new Error('offline');
      }),
    });

    await renderPage(service);

    expect((await screen.findByText(/Whether a backtest is already running could not be checked/)).closest('[role="alert"]')).not.toBeNull();
  });
});

describe('when the page cannot tell whether a backtest is going', () => {
  it('offers to ask again, and shows the backtest that is going once the history answers', async () => {
    const jobs = fakeJobs();
    jobs.register('job-9');
    const runs = [Promise.reject(new Error('offline')), Promise.resolve([runItem({ id: 9, job_id: 'job-9' })])];
    const service = fakeService(vi, { runs: vi.fn(() => runs.shift() ?? Promise.resolve([])), run: vi.fn(async () => going()) });
    await renderPage(service, undefined, { jobs });
    const notice = (await screen.findByText(/Whether a backtest is already running could not be checked/)).closest('[role="alert"]') as HTMLElement;

    press('Try again');

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
    await lettingGo();
    expect(setup.service.run).toHaveBeenCalledTimes(2);
  });

  it('shows the reason and each item when it fails, and lets another be started', async () => {
    const failure = { code: 'DATA_MISSING', reason: 'Some sessions have no data to replay.', items: [{ session_open_ms: runItem().first_session_open_ms, code: 'listing', message: 'No listing was stored.' }] };
    const setup = scriptedRun(vi);
    setup.service.run.mockResolvedValueOnce(going()).mockResolvedValue(runDetail({ status: 'failed', failure, figures: null, slippage: [], slippage_tolerance: null, headline: null }));
    await readyToStart(setup);
    fireEvent.click(startButton());
    await screen.findByRole('region', { name: 'Backtest run' });

    setup.jobs.emit('job-1', { type: 'job.failed', code: 'RunFailedError', message: failure.reason });

    const alert = await screen.findByRole('alert');
    expect(reads(alert)).toBe('Some sessions have no data to replay.');
    expect(reads(screen.getByRole('list', { name: 'What failed' }))).toBe('Listing · 1 item 2026-09-14 – No listing was stored.');
    await waitFor(() => expect(startButton().disabled).toBe(false));
  });

  it('says it was cancelled and that nothing was saved, when Cancel is pressed and the job stops', async () => {
    const setup = scriptedRun(vi);
    setup.service.run.mockResolvedValueOnce(going()).mockResolvedValue(runDetail({ status: 'cancelled', figures: null, slippage: [], slippage_tolerance: null, headline: null }));
    await readyToStart(setup);
    fireEvent.click(startButton());
    const panel = await screen.findByRole('region', { name: 'Backtest run' });

    within(panel).getByRole('button', { name: 'Cancel' }).click();
    await waitFor(() => expect(setup.jobs.cancelled).toEqual(['job-1']));
    setup.jobs.emit('job-1', { type: 'job.cancelled', reason: 'Cancelled.' });

    expect(await within(panel).findByText('The run was cancelled. Nothing was saved.')).toBeTruthy();
    expect(within(panel).queryByText(/You asked to cancel/)).toBeNull();
    await waitFor(() => expect(startButton().disabled).toBe(false));
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
