import { TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';
import { fireEvent, screen, waitFor, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type { SessionClock } from '../../../components/options-backtest/options-backtest.types';
import { fakeJobs } from '../../../components/options-backtest/testing/fake-jobs';
import { fakeService } from '../../../components/options-backtest/testing/fake-service';
import { exampleBody, SESSION_CLOCK, servedExamples, servedSaved } from '../../../components/options-backtest/testing/fixtures';
import { runDetail, runItem } from '../../../components/options-backtest/testing/run-fixtures';
import { goingDetail } from '../../../components/options-backtest/testing/run-script';
import { renderExplore } from '../../testing/render-workspace';

const library = () => [...servedExamples(), servedSaved('Mine', exampleBody())];

describe('Explore before the session clock is read', () => {
  it('cannot put the setup of a run into Define until the clock is read: it says why, and then does it', async () => {
    let release: (clock: SessionClock) => void = () => undefined;
    const complete = runItem({ id: 30, job_id: 'job-30', status: 'complete' });
    const service = fakeService(vi, {
      sessionClock: vi.fn(() => new Promise<SessionClock>((resolve) => (release = resolve))),
      list: vi.fn(async () => library()),
      runs: vi.fn(async () => [complete]),
      run: vi.fn(async () => runDetail({ ...complete })),
    });
    await renderExplore(service, 'Mine', { query: '?view=history&run=30', settled: false });
    const section = await screen.findByRole('region', { name: 'The setup this run used' });
    await within(section).findByText('Window for SPY.');
    const use = within(section).getByRole<HTMLButtonElement>('button', { name: 'Use this setup' });

    expect(use.disabled).toBe(true);
    expect(within(section).getByText(/until the session times have been read/)).toBeTruthy();

    release(SESSION_CLOCK);
    await waitFor(() => expect(use.disabled).toBe(false));
    expect(within(section).queryByText(/until the session times have been read/)).toBeNull();
    fireEvent.click(use);

    await screen.findByRole('heading', { name: 'Entry settings' });
    expect(screen.getByText('The setup of that run was loaded as a new draft. It is not saved under any name.')).toBeTruthy();
    expect(decodeURIComponent(TestBed.inject(Router).url)).toBe('/options-setups/_new/define');
  });

  it('still shows a backtest that is going, with its Cancel, when the clock cannot be read: the run does not depend on it', async () => {
    const jobs = fakeJobs();
    jobs.register('job-7');
    jobs.emit('job-7', { type: 'job.phase', phase: 'replaying', friendly: 'Replaying each session' });
    const service = fakeService(vi, {
      sessionClock: vi.fn(async () => {
        throw new Error('data plane down');
      }),
      list: vi.fn(async () => library()),
      runs: vi.fn(async () => [runItem()]),
      run: vi.fn(async () => goingDetail()),
    });

    await renderExplore(service, 'Mine', { jobs });

    const run = await screen.findByRole('region', { name: 'Backtest run' });
    expect(within(run).getByText('Replaying each session')).toBeTruthy();
    expect(within(run).getByRole('button', { name: 'Cancel' })).toBeTruthy();
  });
});
