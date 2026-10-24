import type { InstrumentDataReport } from '../../shared/data-lake';
import type { TradeBackfillEnd } from '../../shared/data-lake/trade-backfill-runner';

/*
 * What the Data tab does with Python's reading of an instrument's data
 * (`GET /api/data-lake/instrument-data`): the stretches of sessions it lists,
 * and the report of a Fill. Python counts and classifies; this only groups
 * what it classified and compares two of its readings. No calendar, floor or
 * session time is known here.
 */

export type InstrumentSession = InstrumentDataReport['sessions'][number];
export type SessionState = InstrumentSession['state'];
export type SessionBlocker = NonNullable<InstrumentSession['blocked_by']>;

/** The states that are a gap in what a run can read: everything that is neither ready nor not yet finished. */
const GAP_STATES: ReadonlySet<SessionState> = new Set<SessionState>(['needs_rebuild', 'missing', 'failed', 'building']);

/** What makes two neighbouring sessions one gap: the same state, the same cause and the same answer to "can a Fill fetch it". */
export interface GapCause {
  readonly state: SessionState;
  readonly reason: string | null;
  readonly fetchable: boolean;
  readonly blockedBy: SessionBlocker | null;
}

/** Sessions that are consecutive in the calendar and share one cause. */
export interface GapStretch extends GapCause {
  /** Session open of the first and last session, `int64 ms UTC`; render in `date-et`. */
  readonly firstMs: number;
  readonly lastMs: number;
  readonly count: number;
}

function causeOf(session: InstrumentSession): GapCause {
  return {
    state: session.state,
    reason: session.reason ?? null,
    fetchable: session.fetchable ?? false,
    blockedBy: session.blocked_by ?? null,
  };
}

function sameCause(a: GapCause, b: GapCause): boolean {
  return a.state === b.state && a.reason === b.reason && a.fetchable === b.fetchable && a.blockedBy === b.blockedBy;
}

/**
 * The window's gaps, in calendar order, one stretch per run of neighbouring
 * sessions with the same cause. A session that is ready (or not finished yet)
 * ends the stretch before it: two gaps either side of a held session are two
 * gaps. Neighbours are the calendar's own, so a weekend or a holiday between
 * two sessions does not split a stretch.
 */
export function gapStretches(sessions: readonly InstrumentSession[]): readonly GapStretch[] {
  const stretches: GapStretch[] = [];
  // Whether the stretch last pushed is still open: the session before this one was a gap.
  let adjacent = false;
  for (const session of sessions) {
    if (!GAP_STATES.has(session.state)) {
      adjacent = false;
      continue;
    }
    const cause = causeOf(session);
    const last = stretches.at(-1);
    if (adjacent && last !== undefined && sameCause(last, cause)) {
      stretches[stretches.length - 1] = { ...last, lastMs: session.trading_date_ms, count: last.count + 1 };
    } else {
      stretches.push({ ...cause, firstMs: session.trading_date_ms, lastMs: session.trading_date_ms, count: 1 });
    }
    adjacent = true;
  }
  return stretches;
}

/** How a Fill's job ended. A job this place stopped watching (which may still be running) is not an end, and is never reported. */
export type FillEnd = { readonly kind: 'completed' } | Extract<TradeBackfillEnd, { readonly kind: 'failed' | 'cancelled' }>;

/** One cause behind sessions that are still not ready after a Fill, with how many. */
export interface MissingCause extends GapCause {
  readonly count: number;
}

/** What a Fill did, counted in sessions of this request's window: before against after. */
export interface FillReport {
  /** Sessions that were not ready before and are ready now. */
  readonly added: number;
  /** Sessions that were ready before and stayed ready: asked for, found, not fetched again. */
  readonly reused: number;
  /** Sessions still not ready, grouped by what holds each back. */
  readonly missing: readonly MissingCause[];
  readonly missingCount: number;
  /** The job finished and a run can read every finished session. A fill that failed never is. */
  readonly complete: boolean;
}

/**
 * Compares the window before a Fill with the window after it. Both are
 * Python's own readings of the same request, so every session in one is in the
 * other; the report is their difference, and the lake's state after is what
 * says what is missing, whatever the job said.
 */
export function fillReport(before: InstrumentDataReport, after: InstrumentDataReport, end: FillEnd): FillReport {
  const readyBefore = new Set(before.sessions.filter((s) => s.state === 'ready').map((s) => s.trading_date_ms));
  let added = 0;
  let reused = 0;
  const causes: MissingCause[] = [];
  for (const session of after.sessions) {
    if (session.state === 'ready') {
      if (readyBefore.has(session.trading_date_ms)) reused += 1;
      else added += 1;
      continue;
    }
    if (!GAP_STATES.has(session.state)) continue;
    const cause = causeOf(session);
    const known = causes.findIndex((candidate) => sameCause(candidate, cause));
    if (known === -1) causes.push({ ...cause, count: 1 });
    else causes[known] = { ...causes[known], count: causes[known].count + 1 };
  }
  const missingCount = causes.reduce((total, cause) => total + cause.count, 0);
  return {
    added,
    reused,
    missing: causes,
    missingCount,
    complete: end.kind === 'completed' && missingCount === 0 && after.summary.run_readable,
  };
}
