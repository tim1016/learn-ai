import { HttpErrorResponse } from '@angular/common/http';
import { DestroyRef, inject } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { RunRefusedError } from './options-backtest.service';
import type { RunListItem, RunStartRequest } from './options-backtest.types';
import { RunLaunch, type RunApi } from './run-launch';
import { exampleBody } from './testing/fixtures';
import { runItem } from './testing/run-fixtures';

const REQUEST: RunStartRequest = { setup: exampleBody(), start_ms: 1_000, end_ms: 2_000, setup_name: 'Mine' };

/** A launch made inside an injection context, as the page makes it. A look for a record that was not found again is off unless `lookAgainMs` is given. */
function launchOf(service: RunApi, lookAgainMs = 0): RunLaunch {
  return TestBed.runInInjectionContext(() => new RunLaunch(service, inject(DestroyRef), lookAgainMs));
}

function api(changes: Partial<RunApi> = {}): RunApi & { startRun: ReturnType<typeof vi.fn>; runs: ReturnType<typeof vi.fn> } {
  return {
    startRun: vi.fn(async () => 'job-7'),
    runs: vi.fn(async () => [runItem()]),
    ...changes,
  } as RunApi & { startRun: ReturnType<typeof vi.fn>; runs: ReturnType<typeof vi.fn> };
}

const failing = (error: unknown) => launchOf(api({ startRun: vi.fn(async () => Promise.reject(error)) }));

describe('starting a run', () => {
  it('starts the job with the request, finds the run it made by its job id, and shows it', async () => {
    const service = api();
    const launch = launchOf(service);

    await launch.start(REQUEST);

    expect(service.startRun).toHaveBeenCalledWith(REQUEST);
    expect(service.runs).toHaveBeenCalledWith({ jobId: 'job-7' });
    expect(launch.current()).toEqual(runItem());
    expect(launch.starting()).toBe(false);
  });

  it('is busy from the moment it starts to the moment the run is found, and while the run it found is going', async () => {
    let release: (jobId: string) => void = () => undefined;
    const service = api({ startRun: vi.fn(() => new Promise<string>((resolve) => (release = resolve))) });
    const launch = launchOf(service);

    const pending = launch.start(REQUEST);
    expect(launch.busy()).toBe(true);
    expect(launch.starting()).toBe(true);
    release('job-7');
    await pending;

    expect(launch.starting()).toBe(false);
    expect(launch.busy()).toBe(true);
    launch.update(runItem({ status: 'complete' }));
    expect(launch.busy()).toBe(false);
  });

  it('does not start a second run while one is being started or is going', async () => {
    const service = api();
    const launch = launchOf(service);

    const first = launch.start(REQUEST);
    await launch.start(REQUEST);
    await first;
    await launch.start(REQUEST);

    expect(service.startRun).toHaveBeenCalledTimes(1);
  });

  it('starts another once the run on screen has ended', async () => {
    const service = api();
    const launch = launchOf(service);
    await launch.start(REQUEST);
    launch.update(runItem({ status: 'failed' }));

    await launch.start(REQUEST);

    expect(service.startRun).toHaveBeenCalledTimes(2);
  });

  it('keeps a range the server refuses, with its message, and shows no run', async () => {
    const launch = launchOf(
      api({
        startRun: vi.fn(async () => {
          throw new RunRefusedError('OPTIONS_BACKTEST_RANGE_EMPTY', 'The range holds no session.', 400);
        }),
      }),
    );

    await launch.start(REQUEST);

    expect(launch.refusal()).toBe('The range holds no session.');
    expect(launch.problem()).toBeNull();
    expect(launch.current()).toBeNull();
    expect(launch.busy()).toBe(false);
    launch.clearRefusal();
    expect(launch.refusal()).toBeNull();
  });

  it('forgets an earlier refusal and problem when it starts again', async () => {
    let refuse = true;
    const launch = launchOf(
      api({
        startRun: vi.fn(async () => {
          if (refuse) throw new RunRefusedError('X', 'No.', 400);
          return 'job-8';
        }),
      }),
    );
    await launch.start(REQUEST);

    refuse = false;
    await launch.start(REQUEST);

    expect(launch.refusal()).toBeNull();
    expect(launch.current()).not.toBeNull();
  });
});

describe('when the server will not start the backtest, other than for its range', () => {
  it('shows a refusal in the server’s own words and with its code, and does not say to try again, for a refusal trying again cannot cure', async () => {
    const launch = failing(new RunRefusedError('OPTIONS_BACKTEST_JOB_ALREADY_HAS_A_RUN', 'A run was already made for this job.', 409));

    await launch.start(REQUEST);

    expect(launch.problem()).toEqual({ kind: 'not_started', message: 'A run was already made for this job.', code: 'OPTIONS_BACKTEST_JOB_ALREADY_HAS_A_RUN' });
    expect(launch.refusal()).toBeNull();
    expect(launch.busy()).toBe(false);
  });

  it('says to try again, after the server’s own words, for a refusal of the server being unable (a 5xx)', async () => {
    const launch = failing(new RunRefusedError('OPTIONS_BACKTEST_DATA_CATALOG_UNAVAILABLE', 'The data catalog cannot be asked right now.', 503));

    await launch.start(REQUEST);

    expect(launch.problem()).toEqual({ kind: 'not_started', message: 'The data catalog cannot be asked right now. Try again.', code: 'OPTIONS_BACKTEST_DATA_CATALOG_UNAVAILABLE' });
  });

  it.each([
    [400, { error: 'nope' }],
    [499, { error: 'nope' }],
    [422, { error: { detail: JSON.stringify({ detail: [{ loc: ['body'], msg: 'nope' }] }) } }],
  ])('does not say to try again for a %s the server gave no reason for, and does not repeat a body it cannot read', async (status, body) => {
    const launch = failing(new HttpErrorResponse({ status, ...body }));

    await launch.start(REQUEST);

    expect(launch.problem()).toEqual({ kind: 'not_started', message: 'The server did not accept the backtest.', code: null });
    expect(launch.busy()).toBe(false);
  });
});

describe('when the request to start the backtest ends without a clear answer', () => {
  it.each([
    ['a 500', new HttpErrorResponse({ status: 500, error: 'oops' })],
    ['a 502 with no refusal in it', new HttpErrorResponse({ status: 502, error: 'bad gateway' })],
    ['a dropped connection (status 0)', new HttpErrorResponse({ status: 0 })],
    ['an answer that cannot be read', new HttpErrorResponse({ status: 200, error: new SyntaxError('Unexpected token') })],
    ['a failure that is no HTTP answer at all', new Error('the jobs service is down')],
  ])('after %s, does not say the backtest was not started, because it may have been: it points to History, and frees Start without looking for a run', async (_case, error) => {
    const service = api({ startRun: vi.fn(async () => Promise.reject(error)) });
    const launch = launchOf(service);

    await launch.start(REQUEST);

    expect(launch.problem()?.kind).toBe('start_unanswered');
    expect(launch.problem()?.message).toContain('History');
    expect(launch.problem()?.message).not.toContain('could not be started');
    expect(launch.refusal()).toBeNull();
    expect(launch.current()).toBeNull();
    expect(launch.busy()).toBe(false);
    expect(service.runs).not.toHaveBeenCalled();
    await launch.start(REQUEST);
    expect(service.startRun).toHaveBeenCalledTimes(2);
  });
});

describe('a job that started, whose run cannot be found yet', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it.each([
    ['cannot be read', () => Promise.reject(new Error('offline')), 'cannot be read yet'],
    ['is not in the list', () => Promise.resolve([] as RunListItem[]), 'cannot be read yet'],
  ])('says the backtest was started, not that it was not, keeps Start off, and starts no other, when the record %s', async (_case, lookup, words) => {
    const service = api({ runs: vi.fn(lookup) });
    const launch = launchOf(service, 3_000);

    await launch.start(REQUEST);
    await launch.start(REQUEST);
    await launch.start(REQUEST);

    expect(launch.problem()?.kind).toBe('record_unread');
    expect(launch.problem()?.message).toContain('The backtest was started');
    expect(launch.problem()?.message).toContain(words);
    expect(launch.problem()?.message).not.toContain('could not be started');
    expect(launch.starting()).toBe(false);
    expect(launch.busy()).toBe(true);
    expect(launch.current()).toBeNull();
    expect(service.startRun).toHaveBeenCalledTimes(1);
  });

  it('looks again every few seconds until it finds the record, and then shows the run and says nothing of a problem', async () => {
    const lookups = [Promise.reject(new Error('offline')), Promise.resolve([] as RunListItem[]), Promise.resolve([runItem({ status: 'running' })])];
    const service = api({ runs: vi.fn(() => lookups.shift() ?? Promise.resolve([])) });
    const launch = launchOf(service, 3_000);

    await launch.start(REQUEST);
    expect(service.runs).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(2_999);
    expect(service.runs).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(service.runs).toHaveBeenCalledTimes(2);
    expect(launch.busy()).toBe(true);
    await vi.advanceTimersByTimeAsync(3_000);

    expect(service.runs).toHaveBeenCalledTimes(3);
    expect(service.runs).toHaveBeenLastCalledWith({ jobId: 'job-7' });
    expect(launch.current()).toEqual(runItem({ status: 'running' }));
    expect(launch.problem()).toBeNull();
    expect(launch.busy()).toBe(true);
    await vi.advanceTimersByTimeAsync(30_000);
    expect(service.runs).toHaveBeenCalledTimes(3);
    expect(service.startRun).toHaveBeenCalledTimes(1);
  });

  it('frees Start when the record it finds late says the run is already over', async () => {
    const lookups = [Promise.resolve([] as RunListItem[]), Promise.resolve([runItem({ status: 'complete' })])];
    const launch = launchOf(api({ runs: vi.fn(() => lookups.shift() ?? Promise.resolve([])) }), 3_000);

    await launch.start(REQUEST);
    expect(launch.busy()).toBe(true);
    await vi.advanceTimersByTimeAsync(3_000);

    expect(launch.current()?.status).toBe('complete');
    expect(launch.busy()).toBe(false);
  });

  it('keeps looking for as long as the record is not there', async () => {
    const service = api({ runs: vi.fn(async () => []) });
    const launch = launchOf(service, 3_000);

    await launch.start(REQUEST);
    await vi.advanceTimersByTimeAsync(30_000);

    expect(service.runs.mock.calls.length).toBeGreaterThanOrEqual(10);
    expect(launch.busy()).toBe(true);
  });

  it('stops looking when the page is gone', async () => {
    const service = api({ runs: vi.fn(async () => []) });
    const gone: (() => void)[] = [];
    const launch = new RunLaunch(service, { onDestroy: (callback: () => void) => (gone.push(callback), () => undefined) } as unknown as DestroyRef, 3_000);

    await launch.start(REQUEST);
    for (const callback of gone) callback();
    await vi.advanceTimersByTimeAsync(30_000);

    expect(service.runs).toHaveBeenCalledTimes(1);
  });
});

describe('a look for the record that is still on its way when the page goes', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('is not followed by another look', async () => {
    let answer: (runs: RunListItem[]) => void = () => undefined;
    const service = api({ runs: vi.fn(() => new Promise<RunListItem[]>((resolve) => (answer = resolve))) });
    const gone: (() => void)[] = [];
    const launch = new RunLaunch(service, { onDestroy: (callback: () => void) => (gone.push(callback), () => undefined) } as unknown as DestroyRef, 3_000);

    const starting = launch.start(REQUEST);
    await vi.advanceTimersByTimeAsync(0);
    for (const callback of gone) callback();
    answer([]);
    await starting;
    await vi.advanceTimersByTimeAsync(30_000);

    expect(service.runs).toHaveBeenCalledTimes(1);
  });
});

describe('coming back to the page', () => {
  const going = runItem({ id: 9, job_id: 'job-9' });

  it('shows the run that is going, found in the history, and starts nothing', async () => {
    const service = api({ runs: vi.fn(async () => [runItem({ id: 11, status: 'complete' }), going, runItem({ id: 5, status: 'running', job_id: 'job-5' })]) });
    const launch = launchOf(service);

    await launch.takeUp();

    expect(launch.current()).toEqual(going);
    expect(launch.busy()).toBe(true);
    expect(service.startRun).not.toHaveBeenCalled();
  });

  it('shows nothing when no run is going', async () => {
    const launch = launchOf(api({ runs: vi.fn(async () => [runItem({ status: 'complete' }), runItem({ id: 8, status: 'interrupted' })]) }));

    await launch.takeUp();

    expect(launch.current()).toBeNull();
    expect(launch.busy()).toBe(false);
  });

  it('says so when the history cannot be read, because a run might be going', async () => {
    const launch = launchOf(
      api({
        runs: vi.fn(async () => {
          throw new Error('offline');
        }),
      }),
    );

    await launch.takeUp();

    expect(launch.problem()).toEqual({ kind: 'unchecked', message: 'Whether a backtest is already running could not be checked: the history did not answer.', code: null });
  });

  it('forgets that it could not check when it is asked again, and shows the run that is going when the history answers', async () => {
    const answers = [Promise.reject(new Error('offline')), Promise.resolve([going])];
    const launch = launchOf(api({ runs: vi.fn(() => answers.shift() ?? Promise.resolve([])) }));
    await launch.takeUp();
    expect(launch.problem()?.kind).toBe('unchecked');

    await launch.takeUp();

    expect(launch.problem()).toBeNull();
    expect(launch.current()).toEqual(going);
  });

  it('does not replace a run the owner has started while the history was being read', async () => {
    let answer: (runs: RunListItem[]) => void = () => undefined;
    const started = runItem({ id: 20, job_id: 'job-20' });
    const service = api({
      runs: vi.fn((query) => (query.jobId === undefined ? new Promise<RunListItem[]>((resolve) => (answer = resolve)) : Promise.resolve([started]))),
    });
    const launch = launchOf(service);

    const taking = launch.takeUp();
    await launch.start(REQUEST);
    answer([going]);
    await taking;

    expect(launch.current()).toEqual(started);
  });
});

describe('updating the run on screen', () => {
  it('takes a newer record of the same run, and leaves it be for another run', async () => {
    const launch = launchOf(api());
    await launch.start(REQUEST);

    launch.update(runItem({ id: 99, status: 'failed' }));
    expect(launch.current()?.status).toBe('running');
    launch.update(runItem({ status: 'complete' }));

    expect(launch.current()?.status).toBe('complete');
  });
});
