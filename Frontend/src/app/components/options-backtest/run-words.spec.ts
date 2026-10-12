import { describe, expect, it } from 'vitest';

import type { JobState } from '../../services/jobs.service';
import { jobAfter, type Frame } from './testing/fake-jobs';
import { monthLabel, progressView, toleranceSentence } from './run-words';

const job = (changes: Partial<JobState> = {}): JobState => ({ id: 'job-7', type: 'options_backtest', status: 'running', recentLogs: [], logSeq: 0, ...changes });

const phase = (id: string, friendly = 'x'): Frame => ({ event: { type: 'job.phase', phase: id, friendly } });
const progress = (current: number, total: number, id?: string): Frame => ({ event: { type: 'job.progress', current, total, unit: 'sessions' }, id });
const logged = (message: string): Frame => ({ event: { type: 'job.log', message } });

describe('what to show of a job that is going', () => {
  it('says it is waiting for the run’s progress while the jobs service does not know the job', () => {
    expect(progressView(null)).toEqual({ phase: 'Waiting for the run’s progress', counted: false, count: null, latestLog: null });
  });

  it('says the phase as the job labels it', () => {
    expect(progressView(jobAfter(phase('checking_data', 'Checking which sessions can be tested'))).phase).toBe('Checking which sessions can be tested');
  });

  it.each(['checking_data', 'downloading_data', 'replaying'])('counts sessions in the %s phase, once the phase has reported one', (id) => {
    expect(progressView(jobAfter(phase(id), progress(3, 120))).count).toEqual({ current: 3, total: 120, unit: 'sessions' });
  });

  it.each(['checking_data', 'downloading_data', 'replaying'])('shows no count at the start of the %s phase, before it has reported one, though it counts', (id) => {
    const view = progressView(jobAfter(phase(id)));

    expect(view.count).toBeNull();
    expect(view.counted).toBe(true);
  });

  it.each([
    ['checking_data', 'downloading_data'],
    ['downloading_data', 'replaying'],
    ['downloading_data', 'waiting_for_engine'],
    ['replaying', 'saving'],
  ])('shows no count at the start of the phase after %s, though the job holds the last count of that one: %s', (earlier, later) => {
    const view = progressView(jobAfter(phase(earlier), progress(1, 5), progress(5, 5), phase(later)));

    expect(view.count).toBeNull();
  });

  it.each(['waiting_for_engine', 'saving'])('shows no count in the %s phase, and draws no bar, whatever progress came before', (id) => {
    const view = progressView(jobAfter(phase('replaying'), progress(5, 5), phase(id)));

    expect(view.count).toBeNull();
    expect(view.counted).toBe(false);
  });

  it('shows no count from a phase before when the same progress frame is sent again after the job has moved on', () => {
    // The reducer folds a redelivered frame into the job (it reads 5 of 5 again) but does not list it again: it is not news of this phase.
    const job = jobAfter(phase('downloading_data'), progress(5, 5, 'frame-2'), phase('replaying'), progress(5, 5, 'frame-2'));

    expect([job.current, job.total]).toEqual([5, 5]);
    expect(progressView(job).count).toBeNull();
  });

  it('shows the count again once the new phase has reported its own, and then the latest', () => {
    const job = jobAfter(phase('downloading_data'), progress(5, 5), phase('replaying'), progress(1, 5), progress(2, 5));

    expect(progressView(job).count).toEqual({ current: 2, total: 5, unit: 'sessions' });
  });

  it('shows no count for progress that came before the first phase of the job', () => {
    expect(progressView(jobAfter(progress(2, 5), phase('replaying'))).count).toBeNull();
  });

  it('shows the count when the job’s list of events has rolled past the phase event, for every event left came after it', () => {
    const rolled: JobState = { ...jobAfter(phase('replaying'), progress(499, 500), progress(500, 500)), recentEvents: jobAfter(progress(498, 500), progress(499, 500)).recentEvents };

    expect(progressView(rolled).count).toEqual({ current: 500, total: 500, unit: 'sessions' });
  });

  it('shows no count for a job that holds no events at all', () => {
    expect(progressView(job({ phase: 'replaying', phaseLabel: 'x', current: 3, total: 5, unit: 'sessions' })).count).toBeNull();
  });

  it('shows the latest line of the log, and none while there is none', () => {
    expect(progressView(jobAfter(logged('first'), logged('second'))).latestLog).toBe('second');
    expect(progressView(jobAfter()).latestLog).toBeNull();
  });
});

describe('the sentence the slippage tolerance means', () => {
  it('says how far the result survives and where it first loses', () => {
    expect(toleranceSentence({ highest_profitable_percent: '0.3', first_unprofitable_percent: '0.6', nothing_traded: false })).toBe(
      'The result survives up to 0.3% slippage. It is first unprofitable at 0.6%.',
    );
  });

  it('says it survives the whole ladder when no rung lost', () => {
    expect(toleranceSentence({ highest_profitable_percent: '1.0', first_unprofitable_percent: null, nothing_traded: false })).toBe(
      'The result survives the whole ladder, up to 1.0% slippage.',
    );
  });

  it('says it is unprofitable from the lowest rung when none was profitable', () => {
    expect(toleranceSentence({ highest_profitable_percent: null, first_unprofitable_percent: '0.0', nothing_traded: false })).toBe(
      'The result is not profitable even at the lowest slippage tried. It is first unprofitable at 0.0%.',
    );
  });

  it('says nothing traded, which is not the same as a result that lost at every rung', () => {
    expect(toleranceSentence({ highest_profitable_percent: null, first_unprofitable_percent: null, nothing_traded: true })).toBe(
      'Nothing traded, so there is no slippage tolerance to read.',
    );
  });
});

describe('a month’s label', () => {
  it('is the year and the month, the month with two digits', () => {
    expect(monthLabel(2026, 9)).toBe('2026-09');
    expect(monthLabel(2026, 12)).toBe('2026-12');
    expect(monthLabel(2027, 1)).toBe('2027-01');
  });
});
