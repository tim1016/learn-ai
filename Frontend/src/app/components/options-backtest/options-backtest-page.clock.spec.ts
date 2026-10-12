import { fireEvent, render, screen, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { JobsService } from '../../services/jobs.service';
import { fakePickerWorld } from '../../shared/symbol-picker/testing/fake-picker-world';
import { OptionsBacktestPageComponent } from './options-backtest-page.component';
import { OptionsBacktestService } from './options-backtest.service';
import { RUN_POLL_MS } from './run-poll';
import type { SessionClock } from './options-backtest.types';
import { SESSION_CLOCK } from './testing/fixtures';
import { press } from './testing/page-actions';
import { fakeJobs } from './testing/fake-jobs';
import { fakeService } from './testing/render-page';
import { runItem } from './testing/run-fixtures';
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
