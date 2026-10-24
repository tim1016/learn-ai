import { barTimeframeByValue, parseYmdMsUtc } from '../components/data-lab/data-lab-scope';
import { utcDayEndMs } from '../components/data-lab/data-lab-request-mapper';

/*
 * The context an instrument's tabs share: the window, the bar timeframe, the
 * session, the price adjustment and the forward-fill choice (PRD #3490 R09).
 * It lives in the address, so Back, Forward and a reload keep it, and a tab
 * change carries it. The symbol is the address's path and is not part of this
 * value.
 *
 * Dates are the form values the shared date picker hands over (ADR 0075,
 * ADR 0022 (f)): `YYYY-MM-DD`. The host builds int64 ms UTC from them at its
 * own request, and nowhere else.
 */

export type InstrumentSession = 'regular' | 'extended';

/** The lake tree whose prices the Chart and the Data tab read. Returns reads its own (split and dividend adjusted from raw bars). */
export type InstrumentAdjustment = 'polygon_split_adjusted' | 'raw';

/** The default bar timeframe; one candle a day. */
export const DEFAULT_TIMEFRAME = '1d';
export const DEFAULT_SESSION: InstrumentSession = 'regular';
export const DEFAULT_ADJUSTMENT: InstrumentAdjustment = 'polygon_split_adjusted';
/** Whether minute gaps are forward-filled in the chart's bars. */
export const DEFAULT_FORWARD_FILL = true;

/** A window as the date picker holds it: two calendar dates, both included. */
export interface InstrumentWindow {
  readonly from: string;
  readonly to: string;
}

/** What the tabs share besides the window. */
export interface ContextSettings {
  readonly timeframe: string;
  readonly session: InstrumentSession;
  readonly adjustment: InstrumentAdjustment;
  readonly forwardFill: boolean;
}

/** What the tabs share. Every field is committed: none is a draft. */
export interface InstrumentContext extends ContextSettings {
  readonly window: InstrumentWindow;
}

/** The Data tab's lake tree and the chart's price adjustment (`adjustment=raw`; absent reads the split-adjusted tree). */
export const ADJUSTMENT_QUERY_KEY = 'adjustment';
/** Forward fill of the chart's bars (`forwardFill=off`; absent reads it on). */
export const FORWARD_FILL_QUERY_KEY = 'forwardFill';

/** The query keys the context owns. Any other key in the address is left alone. */
export const CONTEXT_QUERY_KEYS = ['from', 'to', 'timeframe', 'session', ADJUSTMENT_QUERY_KEY, FORWARD_FILL_QUERY_KEY] as const;

/**
 * The key that names a saved setup to open under the instrument (a link from
 * the old Data Lab keeps it). It is spent on arrival: the setup's own window,
 * timeframe and session take its place in the address.
 */
export const SETUP_QUERY_KEY = 'sessionId';

/**
 * Every query key the Chart, Returns and Data tabs read. A key outside this
 * list is one those tabs ignore, and the place says so; a tab that starts
 * reading another key declares it here.
 */
const READ_QUERY_KEYS: ReadonlySet<string> = new Set([...CONTEXT_QUERY_KEYS, SETUP_QUERY_KEY]);

/** The keys of an address the Chart, Returns and Data tabs do not read, in the order the address carries them. */
export function unreadQueryKeys(keys: readonly string[]): readonly string[] {
  return [...new Set(keys)].filter((key) => !READ_QUERY_KEYS.has(key));
}

/** What an address says about its context. */
export type ContextRead =
  /** A complete, ordered window with a timeframe and a session the place offers. */
  | { readonly kind: 'ready'; readonly context: InstrumentContext }
  /** The address names no window at all: the place resolves its default and writes it. */
  | ({ readonly kind: 'window-missing' } & ContextSettings)
  /**
   * The address names something unusable. It says what; nothing is
   * substituted for the window. The other settings are the ones the address
   * named when it named one the place offers, else the defaults.
   */
  | ({ readonly kind: 'invalid'; readonly problems: readonly string[] } & ContextSettings);

/** The query read as plain strings; a repeated key reads its first value. */
export interface QueryReader {
  get(name: string): string | null;
}

function isSession(value: string): value is InstrumentSession {
  return value === 'regular' || value === 'extended';
}

function isAdjustment(value: string): value is InstrumentAdjustment {
  return value === 'raw' || value === 'polygon_split_adjusted';
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

  const adjustmentText = query.get(ADJUSTMENT_QUERY_KEY) ?? DEFAULT_ADJUSTMENT;
  const adjustment = isAdjustment(adjustmentText) ? adjustmentText : DEFAULT_ADJUSTMENT;
  if (!isAdjustment(adjustmentText)) problems.push(`The price adjustment "${adjustmentText}" is not one this place offers (raw or polygon_split_adjusted).`);

  const forwardFillText = query.get(FORWARD_FILL_QUERY_KEY) ?? (DEFAULT_FORWARD_FILL ? 'on' : 'off');
  const forwardFillOffered = forwardFillText === 'on' || forwardFillText === 'off';
  const forwardFill = forwardFillOffered ? forwardFillText === 'on' : DEFAULT_FORWARD_FILL;
  if (!forwardFillOffered) problems.push(`The forward-fill choice "${forwardFillText}" is not one this place offers (on or off).`);

  const settings: ContextSettings = { timeframe, session, adjustment, forwardFill };

  const from = query.get('from');
  const to = query.get('to');
  if (from === null && to === null && problems.length === 0) {
    return { kind: 'window-missing', ...settings };
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
    return { kind: 'invalid', problems, ...settings };
  }
  return { kind: 'ready', context: { window: { from, to }, ...settings } };
}

/**
 * The window as the int64 ms UTC the Data Lab commits: the start date's UTC
 * midnight, and the end date's final UTC instant, so one trading date is a
 * non-degenerate window for a half-open reader. `null` for a date that is not one.
 */
export function windowMsUtc(window: InstrumentWindow): { readonly startMsUtc: number; readonly endMsUtc: number } | null {
  const start = parseYmdMsUtc(window.from);
  const end = parseYmdMsUtc(window.to);
  return start === null || end === null ? null : { startMsUtc: start, endMsUtc: utcDayEndMs(end) };
}

/** Whether two contexts commit the same request. */
export function sameContext(a: InstrumentContext, b: InstrumentContext): boolean {
  return a.window.from === b.window.from
    && a.window.to === b.window.to
    && a.timeframe === b.timeframe
    && a.session === b.session
    && a.adjustment === b.adjustment
    && a.forwardFill === b.forwardFill;
}
