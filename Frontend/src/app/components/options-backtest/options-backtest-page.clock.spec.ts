import { fireEvent, render, screen, waitFor, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { JobsService } from '../../services/jobs.service';
import { fakePickerWorld } from '../../shared/symbol-picker/testing/fake-picker-world';
import { OptionsBacktestPageComponent } from './options-backtest-page.component';
import { OptionsBacktestService } from './options-backtest.service';
import { RunDetailService } from './run-detail/run-detail.service';
import { inertRunDetail } from './run-detail/testing/fake-run-detail';
import { RUN_POLL_MS } from './run-poll';
import type { SessionClock } from './options-backtest.types';
import { SESSION_CLOCK } from './testing/fixtures';
import { press } from './testing/page-actions';
import { fakeJobs } from './testing/fake-jobs';
import { fakeService } from './testing/render-page';
import { runDetail, runItem } from './testing/run-fixtures';
import { goingDetail } from './testing/run-script';

describe('before the session clock is read', () => {
  it('shows nothing that holds a time while it is being read', async () => {
    const pending = new Promise<SessionClock>(() => undefined);
    const service = fakeService(vi, { sessionClock: vi.fn(() => pending) });

    await render(OptionsBacktestPageComponent, {
      providers: [...fakePickerWorld().providers, { provide: OptionsBacktestService, useValue: service }],
    });

    expect(screen.queryByRole('heading', { name: 'Entry settings' })).toBeNull();
    expect((screen.getByRole('button', { name: 'Import setup' }) as HTMLButtonElement).disabled).toBe(true);
    expect(service.describe).not.toHaveBeenCalled();
  });

  it('says why the clock could not be read, offers to read it again, and then shows the form', async () => {
    let fail = true;
    const sessionClock = vi.fn(async () => {
      if (fail) throw new Error('data plane down');
      return SESSION_CLOCK;
    });
    const service = fakeService(vi, { sessionClock });

    await render(OptionsBacktestPageComponent, {
      inputs: { describeDebounceMs: 0 },
      providers: [...fakePickerWorld().providers, { provide: OptionsBacktestService, useValue: service }],
    });

    await screen.findByText(/could not be read/i);
    expect(screen.queryByRole('heading', { name: 'Entry settings' })).toBeNull();
    fail = false;
    press('Try again');

    await screen.findByRole('heading', { name: 'Entry settings' });
    expect((screen.getByLabelText('Entry time') as HTMLInputElement).value).not.toBe('');
  });

  it('cannot open a saved setup until the clock is read, because its times need it', async () => {
    let release: (clock: SessionClock) => void = () => undefined;
    const service = fakeService(vi, { sessionClock: vi.fn(() => new Promise<SessionClock>((resolve) => (release = resolve))) });
    await render(OptionsBacktestPageComponent, {
      inputs: { describeDebounceMs: 0 },
      providers: [...fakePickerWorld().providers, { provide: OptionsBacktestService, useValue: service }],
    });
    const side = screen.getByRole('complementary', { name: 'Saved setups' });
    const example = await within(side).findByRole('button', { name: /^SPY premium momentum, no re-entry/ });
    expect((example as HTMLButtonElement).disabled).toBe(true);

    release(SESSION_CLOCK);
    await screen.findByRole('heading', { name: 'Entry settings' });
    fireEvent.click(within(side).getByRole('button', { name: /^SPY premium momentum, no re-entry/ }));

    await screen.findByRole('article', { name: 'Leg 1' });
  });

  it('cannot put the setup of a run in History into the form until the clock is read: it says why, and then does it', async () => {
    let release: (clock: SessionClock) => void = () => undefined;
    const complete = runItem({ id: 30, job_id: 'job-30', status: 'complete' });
    const service = fakeService(vi, {
      sessionClock: vi.fn(() => new Promise<SessionClock>((resolve) => (release = resolve))),
      runs: vi.fn(async () => [complete]),
      run: vi.fn(async () => runDetail({ ...complete })),
    });
    await render(OptionsBacktestPageComponent, {
      inputs: { describeDebounceMs: 0 },
      providers: [
        ...fakePickerWorld().providers,
        { provide: OptionsBacktestService, useValue: service },
        { provide: JobsService, useValue: fakeJobs().service },
        { provide: RunDetailService, useValue: inertRunDetail() },
        { provide: RUN_POLL_MS, useValue: 0 },
      ],
    });
    fireEvent.click(screen.getByRole('tab', { name: 'History' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Open run 30' }));
    const section = await screen.findByRole('region', { name: 'The setup this run used' });
    await within(section).findByText('Window for SPY.');
    const use = within(section).getByRole<HTMLButtonElement>('button', { name: 'Use this setup' });

    expect(use.disabled).toBe(true);
    expect(within(section).getByText(/until the session times have been read/)).toBeTruthy();

    release(SESSION_CLOCK);
    await waitFor(() => expect(use.disabled).toBe(false));
    expect(within(section).queryByText(/until the session times have been read/)).toBeNull();
    use.click();

    await waitFor(() => expect(screen.getByRole('tab', { name: 'Backtest' }).getAttribute('aria-selected')).toBe('true'));
    expect(screen.getByText('The setup of that run was loaded. The dates were not changed.')).toBeTruthy();
  });
});

describe('a backtest that is going, when the session clock cannot be read', () => {
  it('still shows the backtest and its Cancel, since the run does not depend on the clock', async () => {
    const jobs = fakeJobs();
    jobs.register('job-7');
    jobs.emit('job-7', { type: 'job.phase', phase: 'replaying', friendly: 'Replaying each session' });
    const service = fakeService(vi, {
      sessionClock: vi.fn(async () => {
        throw new Error('data plane down');
      }),
      runs: vi.fn(async () => [runItem()]),
      run: vi.fn(async () => goingDetail()),
    });

    await render(OptionsBacktestPageComponent, {
      inputs: { describeDebounceMs: 0 },
      providers: [...fakePickerWorld().providers, { provide: OptionsBacktestService, useValue: service }, { provide: JobsService, useValue: jobs.service }, { provide: RUN_POLL_MS, useValue: 0 }],
    });

    await screen.findByText(/could not be read/i);
    const run = await screen.findByRole('region', { name: 'Backtest run' });
    expect(within(run).getByText('Replaying each session')).toBeTruthy();
    expect(within(run).getByRole('button', { name: 'Cancel' })).toBeTruthy();
    expect(screen.queryByRole('heading', { name: 'Entry settings' })).toBeNull();
  });
});
