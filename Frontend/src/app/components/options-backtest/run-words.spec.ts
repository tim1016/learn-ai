import { describe, expect, it } from 'vitest';

import type { JobState } from '../../services/jobs.service';
import { monthLabel, progressView, toleranceSentence } from './run-words';

const job = (changes: Partial<JobState> = {}): JobState => ({ id: 'job-7', type: 'options_backtest', status: 'running', recentLogs: [], logSeq: 0, ...changes });
const log = (message: string, seq: number) => ({ level: 'info', message, ts: seq, seq });

describe('what to show of a job that is going', () => {
  it('says it is waiting for the run’s progress while the jobs service does not know the job', () => {
    expect(progressView(null)).toEqual({ phase: 'Waiting for the run’s progress', count: null, latestLog: null });
  });

  it('says the phase as the job labels it', () => {
    expect(progressView(job({ phase: 'checking_data', phaseLabel: 'Checking which sessions can be tested' })).phase).toBe('Checking which sessions can be tested');
  });

  it.each(['downloading_data', 'replaying'])('counts sessions in the %s phase', (phase) => {
    expect(progressView(job({ phase, phaseLabel: 'x', current: 3, total: 120, unit: 'sessions' })).count).toEqual({ current: 3, total: 120, unit: 'sessions' });
  });

  it.each(['checking_data', 'waiting_for_engine', 'saving'])('shows no count in the %s phase, even when an earlier phase left one', (phase) => {
    expect(progressView(job({ phase, phaseLabel: 'x', current: 120, total: 120, unit: 'sessions' })).count).toBeNull();
  });

  it('shows no count before the phase has reported one', () => {
    expect(progressView(job({ phase: 'replaying', phaseLabel: 'x' })).count).toBeNull();
  });

  it('shows the latest line of the log, and none while there is none', () => {
    expect(progressView(job({ recentLogs: [log('first', 0), log('second', 1)] })).latestLog).toBe('second');
    expect(progressView(job()).latestLog).toBeNull();
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
