import { parseYmdMsUtc } from '../../../components/data-lab/data-lab-scope';
import { etIsoDate } from '../../../shared/date/et-midnight';
import type { QueryReader } from '../../instrument-context';
import { parseInstantMs } from '../options-address';
import type { ChainModeId, ChainSource } from './chain-modes';

/*
 * What the address asks the chain for (ADR 0084): `?source=(snapshot|live|archive)`, the
 * `expiry` it is of, and, for the past, the session `date` and the instant `at` within it.
 *
 * This is the Options tab's own context. It shares no key with the instrument's window, so
 * moving an option date can never move the chart's window, and a tab change carries the
 * window and leaves these behind.
 *
 * The expiry and the instant are `int64 ms UTC` (an expiry is its 16:00 New York anchor).
 * The session date is the date picker's `YYYY-MM-DD` form value (ADR 0075; ADR 0022 (f)),
 * which the archive host turns into New York midnight at its own request. Nothing here is
 * corrected: an address that asks for something unusable says what, and loads nothing.
 */

const DEFAULT_CHAIN_SOURCE: ChainSource = 'snapshot';

const SOURCES: readonly ChainSource[] = ['snapshot', 'live', 'archive'];

/** What an address asks for. Each kind is one way the chain can be asked, and `invalid` is none of them. */
export type ChainRequest =
  /** Now · Snapshot: the current provider snapshot. `expiryMs` null: the nearest expiry, which the chain says it chose. */
  | { readonly kind: 'snapshot'; readonly expiryMs: number | null }
  /** Past · Archive: `date` null until a session is picked; `atMs` null reads the session's close. */
  | { readonly kind: 'archive'; readonly expiryMs: number | null; readonly date: string | null; readonly atMs: number | null }
  /** Now · Live IBKR. */
  | { readonly kind: 'live'; readonly expiryMs: number | null }
  /** Live IBKR asked for a past date or instant: no such chain exists, and the present's is not shown for it. */
  | { readonly kind: 'past-live'; readonly expiryMs: number | null; readonly date: string | null; readonly atMs: number | null }
  /** The address names something unusable: `problems` say what; `source` is the source it named when it named one. */
  | { readonly kind: 'invalid'; readonly source: ChainSource | null; readonly problems: readonly string[] };

type Part<T> = { readonly ok: true; readonly value: T | null } | { readonly ok: false; readonly problem: string };

function readInstant(text: string | null, what: string): Part<number> {
  if (text === null) return { ok: true, value: null };
  const instant = parseInstantMs(text);
  return instant === null ? { ok: false, problem: `The ${what} "${text}" is not a time in whole milliseconds.` } : { ok: true, value: instant };
}

function readDate(text: string | null): Part<string> {
  if (text === null) return { ok: true, value: null };
  return parseYmdMsUtc(text) === null
    ? { ok: false, problem: `The session date "${text}" is not a date (YYYY-MM-DD).` }
    : { ok: true, value: text };
}

/** Reads the chain request an address carries. */
export function readChainRequest(query: QueryReader): ChainRequest {
  const sourceText = query.get('source') ?? DEFAULT_CHAIN_SOURCE;
  const source = SOURCES.find((candidate) => candidate === sourceText) ?? null;
  const expiry = readInstant(query.get('expiry'), 'expiry');
  const date = readDate(query.get('date'));
  const at = readInstant(query.get('at'), 'instant');

  const problems: string[] = [];
  if (source === null) problems.push(`The source "${sourceText}" is not one this chain offers: snapshot, live or archive.`);
  for (const part of [expiry, date, at]) if (!part.ok) problems.push(part.problem);
  if (!expiry.ok || !date.ok || !at.ok) return { kind: 'invalid', source, problems };

  const expiryMs = expiry.value;
  const atMs = at.value;
  if (atMs !== null && date.value === null) problems.push('A time of day needs the session date it falls on.');
  if (atMs !== null && date.value !== null && etIsoDate(atMs) !== date.value) {
    problems.push(`The instant is not on the session date ${date.value} in New York.`);
  }
  const asksForThePast = date.value !== null || atMs !== null;
  if (source === 'snapshot' && asksForThePast) {
    problems.push('A session date or an instant names the past, and Now · Snapshot is the current chain only. Choose Past · Archive to read a past date.');
  }
  if (source === null || problems.length > 0) return { kind: 'invalid', source, problems };

  if (source === 'archive') return { kind: 'archive', expiryMs, date: date.value, atMs };
  if (source === 'live') {
    return asksForThePast ? { kind: 'past-live', expiryMs, date: date.value, atMs } : { kind: 'live', expiryMs };
  }
  return { kind: 'snapshot', expiryMs };
}

/** The mode a request asks for, or `null` for one that names no usable source. */
export function chainModeOf(request: ChainRequest): ChainModeId | null {
  switch (request.kind) {
    case 'snapshot':
      return 'now-snapshot';
    case 'archive':
      return 'past-archive';
    case 'live':
      return 'now-live';
    case 'past-live':
      return 'past-live';
    case 'invalid':
      return request.source === null ? null : request.source === 'archive' ? 'past-archive' : request.source === 'live' ? 'now-live' : 'now-snapshot';
  }
}
