import { barTimeframeByValue, parseYmdMsUtc } from '../components/data-lab/data-lab-scope';

/*
 * The context an instrument's tabs share: the window, the bar timeframe and
 * the session (PRD #3490 R09). It lives in the address, so Back, Forward and a
 * reload keep it, and a tab change carries it. The symbol is the address's
 * path and is not part of this value.
 *
 * Dates are the form values the shared date picker hands over (ADR 0075,
 * ADR 0022 (f)): `YYYY-MM-DD`. The host builds int64 ms UTC from them at its
 * own request, and nowhere else.
 */

export type InstrumentSession = 'regular' | 'extended';

/** The default bar timeframe; one candle a day. */
export const DEFAULT_TIMEFRAME = '1d';
export const DEFAULT_SESSION: InstrumentSession = 'regular';

/** A window as the date picker holds it: two calendar dates, both included. */
export interface InstrumentWindow {
  readonly from: string;
  readonly to: string;
}

/** What the tabs share. Every field is committed: none is a draft. */
export interface InstrumentContext {
  readonly window: InstrumentWindow;
  readonly timeframe: string;
  readonly session: InstrumentSession;
}

/** The query keys the context owns. Any other key in the address is left alone. */
export const CONTEXT_QUERY_KEYS = ['from', 'to', 'timeframe', 'session'] as const;

/** What an address says about its context. */
export type ContextRead =
  /** A complete, ordered window with a timeframe and a session the place offers. */
  | { readonly kind: 'ready'; readonly context: InstrumentContext }
  /** The address names no window at all: the place resolves its default and writes it. */
  | { readonly kind: 'window-missing'; readonly timeframe: string; readonly session: InstrumentSession }
  /**
   * The address names something unusable. It says what; nothing is
   * substituted for the window. The timeframe and session are the ones the
   * address named when it named one the place offers, else the defaults.
   */
  | {
      readonly kind: 'invalid';
      readonly problems: readonly string[];
      readonly timeframe: string;
      readonly session: InstrumentSession;
    };

/** The query read as plain strings; a repeated key reads its first value. */
export interface QueryReader {
  get(name: string): string | null;
}

function isSession(value: string): value is InstrumentSession {
  return value === 'regular' || value === 'extended';
}

/**
 * Reads the context an address carries.
 *
 * An absent window is not an error: it is how an instrument opens from the
 * Markets list. A window that is half there, unreal, or reversed is an error,
 * and so is a timeframe or session the place does not offer. Nothing is
 * corrected: the caller shows the reason and starts no request.
 */
export function readInstrumentContext(query: QueryReader): ContextRead {
  const problems: string[] = [];

  const timeframeText = query.get('timeframe') ?? DEFAULT_TIMEFRAME;
  const timeframeOffered = barTimeframeByValue(timeframeText) !== undefined;
  if (!timeframeOffered) problems.push(`The bar timeframe "${timeframeText}" is not one this place offers.`);
  const timeframe = timeframeOffered ? timeframeText : DEFAULT_TIMEFRAME;

  const sessionText = query.get('session') ?? DEFAULT_SESSION;
  const session = isSession(sessionText) ? sessionText : DEFAULT_SESSION;
  if (!isSession(sessionText)) problems.push(`The session "${sessionText}" is not one this place offers.`);

  const from = query.get('from');
  const to = query.get('to');
  if (from === null && to === null && problems.length === 0) {
    return { kind: 'window-missing', timeframe, session };
  }
  if (from === null || to === null) {
    if (from !== null || to !== null) problems.push('A window needs both a start date and an end date.');
  } else {
    const start = parseYmdMsUtc(from);
    const end = parseYmdMsUtc(to);
    if (start === null) problems.push(`The start date "${from}" is not a date (YYYY-MM-DD).`);
    if (end === null) problems.push(`The end date "${to}" is not a date (YYYY-MM-DD).`);
    if (start !== null && end !== null && start > end) problems.push('The start date is after the end date.');
  }

  if (problems.length > 0 || from === null || to === null) {
    return { kind: 'invalid', problems, timeframe, session };
  }
  return { kind: 'ready', context: { window: { from, to }, timeframe, session } };
}

/** The query a context writes: only the keys the context owns. */
export function contextQuery(context: InstrumentContext): Record<string, string> {
  return {
    from: context.window.from,
    to: context.window.to,
    timeframe: context.timeframe,
    session: context.session,
  };
}

/** Whether two contexts commit the same request. */
export function sameContext(a: InstrumentContext, b: InstrumentContext): boolean {
  return a.window.from === b.window.from
    && a.window.to === b.window.to
    && a.timeframe === b.timeframe
    && a.session === b.session;
}
