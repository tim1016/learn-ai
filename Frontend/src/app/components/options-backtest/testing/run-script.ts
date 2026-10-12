import type { vi as Vi } from 'vitest';

import type { RunDetail } from '../options-backtest.types';
import { fakeJobs, type FakeJobs } from './fake-jobs';
import { fakeService, type FakeService } from './render-page';
import { runDetail, runItem } from './run-fixtures';

type Spy = Pick<typeof Vi, 'fn'>;

/** A run as the server reads it while its job is going. */
export function goingDetail(): RunDetail {
  return runDetail({ status: 'running', finished_at_ms: null, figures: null, slippage: [], slippage_tolerance: null, headline: null });
}

/**
 * A service and a jobs service that behave as the real ones do when a run starts: the job is registered with the jobs
 * service, the run exists as running and is found by its job id (`job-1`), and the run reads as going until a spec says
 * otherwise.
 */
export function scriptedRun(spy: Spy, changes: Partial<FakeService> = {}): { service: FakeService; jobs: FakeJobs } {
  const jobs = fakeJobs();
  const service = fakeService(spy, {
    startRun: spy.fn(async () => {
      jobs.register('job-1');
      return 'job-1';
    }),
    runs: spy.fn(async (query) => (query?.jobId === 'job-1' ? [runItem({ job_id: 'job-1' })] : [])),
    run: spy.fn(async () => goingDetail()),
    ...changes,
  });
  return { service, jobs };
}
