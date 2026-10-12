import { describe, expect, it, vi } from 'vitest';

import { RunRefusedError } from './options-backtest.service';
import type { RunListItem, RunStartRequest } from './options-backtest.types';
import { RunLaunch, type RunApi } from './run-launch';
import { exampleBody } from './testing/fixtures';
import { runItem } from './testing/run-fixtures';

const REQUEST: RunStartRequest = { setup: exampleBody(), start_ms: 1_000, end_ms: 2_000, setup_name: 'Mine' };

function api(changes: Partial<RunApi> = {}): RunApi & { startRun: ReturnType<typeof vi.fn>; runs: ReturnType<typeof vi.fn> } {
  return {
    startRun: vi.fn(async () => 'job-7'),
    runs: vi.fn(async () => [runItem()]),
    ...changes,
  } as RunApi & { startRun: ReturnType<typeof vi.fn>; runs: ReturnType<typeof vi.fn> };
}

describe('starting a run', () => {
  it('starts the job with the request, finds the run it made by its job id, and shows it', async () => {
    const service = api();
    const launch = new RunLaunch(service);

    await launch.start(REQUEST);

    expect(service.startRun).toHaveBeenCalledWith(REQUEST);
    expect(service.runs).toHaveBeenCalledWith({ jobId: 'job-7' });
    expect(launch.current()).toEqual(runItem());
    expect(launch.starting()).toBe(false);
  });

  it('is busy from the moment it starts to the moment the run is found, and while the run it found is going', async () => {
    let release: (jobId: string) => void = () => undefined;
    const service = api({ startRun: vi.fn(() => new Promise<string>((resolve) => (release = resolve))) });
    const launch = new RunLaunch(service);

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
    const launch = new RunLaunch(service);

    const first = launch.start(REQUEST);
    await launch.start(REQUEST);
    await first;
    await launch.start(REQUEST);

    expect(service.startRun).toHaveBeenCalledTimes(1);
  });

  it('starts another once the run on screen has ended', async () => {
    const service = api();
    const launch = new RunLaunch(service);
    await launch.start(REQUEST);
    launch.update(runItem({ status: 'failed' }));

    await launch.start(REQUEST);

    expect(service.startRun).toHaveBeenCalledTimes(2);
  });

  it('keeps a range the server refuses, with its message, and shows no run', async () => {
    const launch = new RunLaunch(
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

  it('says plainly when the run could not be started for any other reason', async () => {
    const launch = new RunLaunch(
      api({
        startRun: vi.fn(async () => {
          throw new Error('the jobs service is down');
        }),
      }),
    );

    await launch.start(REQUEST);

    expect(launch.problem()).toBe('The backtest could not be started. Try again.');
    expect(launch.refusal()).toBeNull();
    expect(launch.busy()).toBe(false);
  });

  it('says so when the job started and its record cannot be found', async () => {
    const launch = new RunLaunch(api({ runs: vi.fn(async () => []) }));

    await launch.start(REQUEST);

    expect(launch.problem()).toContain('Look for it in History');
    expect(launch.current()).toBeNull();
  });

  it('forgets an earlier refusal and problem when it starts again', async () => {
    let refuse = true;
    const launch = new RunLaunch(
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

describe('coming back to the page', () => {
  const going = runItem({ id: 9, job_id: 'job-9' });

  it('shows the run that is going, found in the history, and starts nothing', async () => {
    const service = api({ runs: vi.fn(async () => [runItem({ id: 11, status: 'complete' }), going, runItem({ id: 5, status: 'running', job_id: 'job-5' })]) });
    const launch = new RunLaunch(service);

    await launch.takeUp();

    expect(launch.current()).toEqual(going);
    expect(launch.busy()).toBe(true);
    expect(service.startRun).not.toHaveBeenCalled();
  });

  it('shows nothing when no run is going', async () => {
    const launch = new RunLaunch(api({ runs: vi.fn(async () => [runItem({ status: 'complete' }), runItem({ id: 8, status: 'interrupted' })]) }));

    await launch.takeUp();

    expect(launch.current()).toBeNull();
    expect(launch.busy()).toBe(false);
  });

  it('says so when the history cannot be read, because a run might be going', async () => {
    const launch = new RunLaunch(
      api({
        runs: vi.fn(async () => {
          throw new Error('offline');
        }),
      }),
    );

    await launch.takeUp();

    expect(launch.problem()).toBe('Whether a backtest is already running could not be checked: the history did not answer.');
  });

  it('does not replace a run the owner has started while the history was being read', async () => {
    let answer: (runs: RunListItem[]) => void = () => undefined;
    const started = runItem({ id: 20, job_id: 'job-20' });
    const service = api({
      runs: vi.fn((query) => (query.jobId === undefined ? new Promise<RunListItem[]>((resolve) => (answer = resolve)) : Promise.resolve([started]))),
    });
    const launch = new RunLaunch(service);

    const taking = launch.takeUp();
    await launch.start(REQUEST);
    answer([going]);
    await taking;

    expect(launch.current()).toEqual(started);
  });
});

describe('updating the run on screen', () => {
  it('takes a newer record of the same run, and leaves it be for another run', async () => {
    const launch = new RunLaunch(api());
    await launch.start(REQUEST);

    launch.update(runItem({ id: 99, status: 'failed' }));
    expect(launch.current()?.status).toBe('running');
    launch.update(runItem({ status: 'complete' }));

    expect(launch.current()?.status).toBe('complete');
  });
});
