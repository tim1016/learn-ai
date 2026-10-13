import { formatReceiptLabel } from '../../shared/pipes/receipt-label.pipe';
import type { JobEventRecord, JobState } from '../../services/jobs.service';
import type { CapitalEvidence, CapitalInvalidEvent, EntryRefusalCount, SlippageTolerance } from './options-backtest.types';

/**
 * Words and small shapes for a run on screen. Every figure stays the
 * server's; what is made here is a sentence around one, or which part of a
 * job's progress to show.
 */

/** The phases whose progress counts sessions: the check, the download and the replay each report `(done, total)`, in sessions. */
const COUNTED_PHASES: readonly string[] = ['checking_data', 'downloading_data', 'replaying'];

export interface ProgressView {
  /** The phase in words, as the job labels it. */
  readonly phase: string;
  /** The phase counts sessions, whether or not it has counted one yet; the bar is then drawn, and is not filled until it has. */
  readonly counted: boolean;
  /** The sessions done of the sessions in the phase, once the phase has reported them. */
  readonly count: { readonly current: number; readonly total: number; readonly unit: string } | null;
  readonly latestLog: string | null;
}

/**
 * What to show of a job that is going. A `job.progress` event carries no phase, and the job keeps the last count it
 * was given when it moves on, so the count belongs to the phase on show only if it was reported after that phase began.
 * The order of the job's events says so, and the page does no arithmetic to find out.
 */
export function progressView(job: JobState | null): ProgressView {
  if (job === null) return { phase: 'Waiting for the run’s progress', counted: false, count: null, latestLog: null };
  const counted = job.phase !== undefined && COUNTED_PHASES.includes(job.phase);
  const reported = counted && reportedSincePhaseBegan(job.recentEvents) && job.current !== undefined && job.total !== undefined;
  return {
    phase: job.phaseLabel ?? 'Starting',
    counted,
    count: reported ? { current: job.current ?? 0, total: job.total ?? 0, unit: job.unit ?? 'sessions' } : null,
    latestLog: job.recentLogs.at(-1)?.message ?? null,
  };
}

/**
 * Whether the latest of the job's phase and progress events is a progress event. The job keeps its last 500 events, so
 * on a long phase the phase event itself may have rolled off the list; then every event left came after it.
 */
function reportedSincePhaseBegan(events: readonly JobEventRecord[] | undefined): boolean {
  for (let at = (events?.length ?? 0) - 1; at >= 0; at -= 1) {
    const type = events?.[at]?.type;
    if (type === 'job.progress') return true;
    if (type === 'job.phase') return false;
  }
  return false;
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

/** How far the cash carried the run, in one sentence. The two numbers are the server's. */
export function fundedThroughSentence(evidence: CapitalEvidence): string {
  const { sessions_funded_continuously: through, sessions_tested: of } = evidence;
  if (of === 0) return 'No session was tested, so there is no cash to judge.';
  if (evidence.ended_by == null) return `Funded continuously through session ${of} of ${of}: the cash carried the whole run.`;
  if (through === 0) return `Not funded continuously through even the first of ${of} sessions.`;
  return `Funded continuously through session ${through} of ${of}.`;
}

/** What a ladder row says ended its evidence, in a few words for a table cell. A kind with no words is shown as a receipt label. */
export function capitalEventLabel(kind: CapitalInvalidEvent['kind']): string {
  switch (kind) {
    case 'unfunded_close':
      return 'an unfunded close';
    case 'position_marked':
      return 'a position marked';
    default:
      return formatReceiptLabel(kind);
  }
}

const REQUIREMENT_RULE_WORDS: Readonly<Record<string, string>> = {
  alpaca_covered_positions: 'Alpaca’s published rule for covered positions',
};

/** The requirement rule's name in words. A rule this page has no words for is shown as a receipt label, never as its raw token. */
export function requirementRuleWords(rule: string): string {
  return REQUIREMENT_RULE_WORDS[rule] ?? formatReceiptLabel(rule);
}

/** A month as `YYYY-MM`, from the year and month the server counts it by. */
export function monthLabel(year: number, month: number): string {
  return `${year}-${String(month).padStart(2, '0')}`;
}

/**
 * The reasons an entry is refused for, apart from cash, each with its words and in the order they are read (#3109). The
 * cover that did not trade is an illiquid wing and the cover not yet held is a rule of the setup, so the two are told apart.
 */
const ENTRY_REFUSAL_WORDS: Readonly<Record<string, string>> = {
  cover_did_not_trade: 'Entries refused: their cover did not trade',
  cover_not_yet_held: 'Entries refused: their cover was not yet held',
  second_expiry: 'Entries refused for a second expiry',
  other_side: 'Entries refused for a contract held on the other side',
  sale_at_zero: 'Sales to open not taken at zero',
};

/** One count of "What did not trade": what it counts and how many, or null when the run was saved before it was counted. */
export interface RefusalRow {
  readonly reason: string;
  readonly label: string;
  readonly count: number | null;
}

/** A reason in words. A reason this page has no words for is shown as a receipt label, never as its raw token. */
export function entryRefusalWords(reason: string): string {
  return ENTRY_REFUSAL_WORDS[reason] ?? formatReceiptLabel(reason);
}

/**
 * Each refusal of an entry with its count, in the order they are read. A run saved before they were counted has none
 * to show, and each reads as not recorded; that is not a count of zero.
 */
export function entryRefusalRows(counts: readonly EntryRefusalCount[] | null | undefined): readonly RefusalRow[] {
  const known = Object.keys(ENTRY_REFUSAL_WORDS);
  if (counts == null) return known.map((reason) => ({ reason, label: entryRefusalWords(reason), count: null }));
  const counted = new Map<string, number>(counts.map((each) => [each.reason, each.count]));
  const reasons = [...known.filter((reason) => counted.has(reason)), ...[...counted.keys()].filter((reason) => !known.includes(reason))];
  return reasons.map((reason) => ({ reason, label: entryRefusalWords(reason), count: counted.get(reason) ?? 0 }));
}
