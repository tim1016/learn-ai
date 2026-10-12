import type { JobState } from '../../services/jobs.service';
import type { SlippageTolerance } from './options-backtest.types';

/**
 * Words and small shapes for a run on screen. Every figure stays the
 * server's; what is made here is a sentence around one, or which part of a
 * job's progress to show.
 */

/** The phases whose progress counts sessions: the job reports 1..N twice, once for the download and once for the replay. */
const COUNTED_PHASES: readonly string[] = ['downloading_data', 'replaying'];

export interface ProgressView {
  /** The phase in words, as the job labels it. */
  readonly phase: string;
  /** The sessions done of the sessions in the phase, only in a phase that counts them. */
  readonly count: { readonly current: number; readonly total: number; readonly unit: string } | null;
  readonly latestLog: string | null;
}

/** What to show of a job that is going. A count belongs to the phase that reported it, so it is hidden once the job moves to another. */
export function progressView(job: JobState | null): ProgressView {
  if (job === null) return { phase: 'Waiting for the run’s progress', count: null, latestLog: null };
  const counted = job.phase !== undefined && COUNTED_PHASES.includes(job.phase) && job.current !== undefined && job.total !== undefined;
  return {
    phase: job.phaseLabel ?? 'Starting',
    count: counted ? { current: job.current ?? 0, total: job.total ?? 0, unit: job.unit ?? 'sessions' } : null,
    latestLog: job.recentLogs.at(-1)?.message ?? null,
  };
}

/** What the slippage ladder says of the result, in one sentence. The percents are the server's text. */
export function toleranceSentence(tolerance: SlippageTolerance): string {
  const { highest_profitable_percent: highest, first_unprofitable_percent: first } = tolerance;
  if (tolerance.nothing_traded) return 'Nothing traded, so there is no slippage tolerance to read.';
  if (highest != null && first != null) return `The result survives up to ${highest}% slippage. It is first unprofitable at ${first}%.`;
  if (highest != null) return `The result survives the whole ladder, up to ${highest}% slippage.`;
  if (first != null) return `The result is not profitable even at the lowest slippage tried. It is first unprofitable at ${first}%.`;
  return 'The server gave no slippage tolerance for this run.';
}

/** A month as `YYYY-MM`, from the year and month the server counts it by. */
export function monthLabel(year: number, month: number): string {
  return `${year}-${String(month).padStart(2, '0')}`;
}
