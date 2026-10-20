import { render, screen } from '@testing-library/angular';

import {
  applyJobEvent,
  describeJobEvent,
  JobsService,
  type JobEvent,
  type JobState,
} from '../../services/jobs.service';
import { WaitingBadgeComponent } from './waiting-badge.component';

function runningJob(waiting: JobState['waiting'], status = 'running'): JobState {
  return { id: 'job-1', type: 'grid_search', status, waiting, recentLogs: [], eventSeq: 0, recentEvents: [] } as unknown as JobState;
}

function serviceOf(job: JobState): JobsService {
  return { job: () => job } as unknown as JobsService;
}

describe('WaitingBadgeComponent (the #3052 waiting states)', () => {
  it('names capacity waiting and adaptive waiting distinctly', async () => {
    await render(WaitingBadgeComponent, {
      providers: [{ provide: JobsService, useValue: serviceOf(runningJob('capacity')) }],
      componentInputs: { jobId: 'job-1' },
    });
    expect(screen.getByRole('status').textContent).toContain('Waiting for machine capacity');
  });

  it('names adaptive waiting distinctly from capacity', async () => {
    const rendered = await render(WaitingBadgeComponent, {
      providers: [{ provide: JobsService, useValue: serviceOf(runningJob('adaptive')) }],
      componentInputs: { jobId: 'job-1' },
    });
    expect(rendered.fixture.nativeElement.textContent).toContain('Waiting on dependent results');
    expect(rendered.fixture.nativeElement.textContent).not.toContain('machine capacity');
  });

  it.each<[string, JobState['waiting'], string]>([
    ['for a run that is evaluating', null, 'running'],
    ['once the run is no longer running', 'capacity', 'completed'],
  ])('renders nothing %s', async (_when, waiting, status) => {
    const rendered = await render(WaitingBadgeComponent, {
      providers: [{ provide: JobsService, useValue: serviceOf(runningJob(waiting, status)) }],
      componentInputs: { jobId: 'job-1' },
    });
    expect(rendered.fixture.nativeElement.textContent).not.toContain('Waiting');
  });
});

describe('the additive event contract (#3052)', () => {
  const base = { id: 'job-2', type: 'grid_search', status: 'running', recentLogs: [], eventSeq: 0, recentEvents: [] } as unknown as JobState;

  it('folds the waiting transitions and clears them on terminals', () => {
    let state = applyJobEvent(base, { type: 'job.research_waiting', waiting: 'capacity' } as unknown as JobEvent);
    expect(state.waiting).toBe('capacity');
    state = applyJobEvent(state, { type: 'job.research_waiting', waiting: null } as unknown as JobEvent);
    expect(state.waiting).toBeNull();
    state = applyJobEvent(state, { type: 'job.research_waiting', waiting: 'adaptive' } as unknown as JobEvent);
    state = applyJobEvent(state, { type: 'job.completed', result_url: '/r' } as unknown as JobEvent);
    expect(state.waiting).toBeNull();
  });

  it('rides additive count and waiting fields on ordinary progress without touching the totals', () => {
    const state = applyJobEvent(base, {
      type: 'job.progress',
      current: 3,
      total: 8,
      unit: 'cells',
      completed: 2,
      reused: 1,
    } as unknown as JobEvent);
    expect([state.current, state.total, state.unit]).toEqual([3, 8, 'cells']);
    expect(state.counts).toMatchObject({ completed: 2, reused: 1 });
    expect(state.waiting).toBeNull();
  });

  it('describes the waiting transitions concisely', () => {
    expect(describeJobEvent({ type: 'job.research_waiting', waiting: 'capacity' } as unknown as JobEvent)).toContain('capacity');
    expect(describeJobEvent({ type: 'job.research_waiting', waiting: 'adaptive' } as unknown as JobEvent)).toContain('dependent results');
    expect(describeJobEvent({ type: 'job.research_waiting', waiting: null } as unknown as JobEvent)).toContain('Evaluating');
  });
});
