import { computed, signal, type Signal } from '@angular/core';

import { applyJobEvent, type JobEvent, type JobState } from '../../../services/jobs.service';

/**
 * A stand-in for the shared jobs service that plays a job's stream from a
 * script. Each event goes through the real reducer (`applyJobEvent`), so the
 * state a page reads is the state the real service would have built from the
 * same frames. It imports nothing from vitest, as a shared test helper must not.
 */
export interface FakeJobs {
  /** What `JobsService` is provided as. */
  readonly service: {
    job(id: string): JobState | undefined;
    readonly activeJobs: Signal<JobState[]>;
    cancelJob(id: string): Promise<void>;
    refreshActive(): Promise<void>;
  };
  /** The ids a cancel was asked for, in order. */
  readonly cancelled: string[];
  /** How many times the jobs that are going were asked for. */
  refreshes: number;
  /** A cancel the server cannot take: the next ones reject with this. */
  failCancel: Error | null;
  /** The job as the real service holds it the moment it has started one. */
  register(id: string, type?: string): void;
  /** A job another tab started: unknown here until the jobs that are going are asked for. */
  registerOnRefresh(id: string, type?: string): void;
  /** One frame of the job's stream. */
  emit(id: string, event: JobEvent): void;
}

export function fakeJobs(): FakeJobs {
  const jobs = signal<ReadonlyMap<string, JobState>>(new Map());
  const waiting: { id: string; type: string }[] = [];
  const add = (id: string, type: string) => {
    const state: JobState = { id, type, status: 'queued', recentLogs: [], logSeq: 0 };
    jobs.update((known) => new Map(known).set(id, state));
  };
  const fake: FakeJobs = {
    cancelled: [],
    refreshes: 0,
    failCancel: null,
    service: {
      job: (id) => jobs().get(id),
      activeJobs: computed(() => [...jobs().values()].filter((job) => job.status === 'queued' || job.status === 'running')),
      cancelJob: async (id) => {
        fake.cancelled.push(id);
        if (fake.failCancel !== null) throw fake.failCancel;
      },
      refreshActive: async () => {
        fake.refreshes += 1;
        for (const job of waiting.splice(0)) add(job.id, job.type);
      },
    },
    register: (id, type = 'options_backtest') => add(id, type),
    registerOnRefresh: (id, type = 'options_backtest') => {
      waiting.push({ id, type });
    },
    emit: (id, event) => {
      jobs.update((known) => {
        const previous = known.get(id);
        if (previous === undefined) throw new Error(`The fake jobs service was never told of job ${id}.`);
        return new Map(known).set(id, applyJobEvent(previous, event));
      });
    },
  };
  return fake;
}

/** One frame of a job's stream, with the id it came with when the same frame may come twice. */
export interface Frame {
  readonly event: JobEvent;
  readonly id?: string;
}

/**
 * A job as the shared reducer holds it after these frames, in this order. A frame without an id is a new one; a frame
 * whose id was met before is a redelivery, which the reducer folds in but does not list again.
 */
export function jobAfter(...frames: Frame[]): JobState {
  let state: JobState = { id: 'job-7', type: 'options_backtest', status: 'queued', recentLogs: [], logSeq: 0 };
  for (const frame of frames) state = applyJobEvent(state, frame.event, frame.id);
  return state;
}
