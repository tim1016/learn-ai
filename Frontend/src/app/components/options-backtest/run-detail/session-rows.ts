import type { OptionContract } from '../../../shared/option-contract/option-contract.component';
import type { SessionDetail, SessionEvent, SessionFill, SessionLevel, SessionMark, TradeContract } from './run-detail.types';
import { attemptWords, contractOf, FILL_WORDS, knownContracts, LEVEL_WORDS, MARK_WORDS } from './words';

/**
 * The rows of the lists beside the one-day chart. They are the chart's text equivalent: every fact the chart shows (each
 * level with its two instants, each fill, each mark) and every event is a row here, with the server's own text. A contract
 * is named from the parsed fields the server sent for it (found by its OCC symbol among the day's trades and series); where
 * the server sent none, the symbol is shown as it came. The page reads no symbol to find these.
 */

/** The instrument a row is about: the contract in words when the server sent its parts, else the symbol as it came, else nothing. */
export interface Instrument {
  readonly contract: OptionContract | null;
  readonly symbol: string | null;
}

export interface EventRow {
  readonly key: string;
  readonly event: SessionEvent;
  readonly instrument: Instrument;
  readonly attempt: string;
}

export interface LevelRow {
  readonly key: string;
  readonly level: SessionLevel;
  readonly name: string;
  readonly drawn: string;
  readonly instrument: Instrument;
  readonly attempt: string;
}

export interface FillRow {
  readonly key: string;
  readonly fill: SessionFill;
  readonly name: string;
  readonly drawn: string;
  readonly instrument: Instrument;
  readonly attempt: string;
}

export interface MarkRow {
  readonly key: string;
  readonly mark: SessionMark;
  readonly name: string;
  readonly drawn: string;
  readonly instrument: Instrument;
  readonly attempt: string;
}

function instrumentOf(symbol: string | null, known: ReadonlyMap<string, TradeContract>): Instrument {
  if (symbol === null) return { contract: null, symbol: null };
  const contract = known.get(symbol);
  return contract === undefined ? { contract: null, symbol } : { contract: contractOf(contract), symbol };
}

/** What happened, in the order the server gave. The same instant can hold several events, so a row's key is its place. */
export function eventRows(session: SessionDetail): EventRow[] {
  const known = knownContracts(session);
  return session.events.map((event, place) => ({
    key: `${place}:${event.at_ms}:${event.kind}`,
    event,
    instrument: instrumentOf(event.contract ?? null, known),
    attempt: attemptWords(event.entry_number),
  }));
}

export function levelRows(session: SessionDetail): LevelRow[] {
  const known = knownContracts(session);
  return session.levels.map((level, place) => ({
    key: `${place}:${level.contract}:${level.leg_id}:${level.entry_number}:${level.kind}`,
    level,
    name: LEVEL_WORDS[level.kind].name,
    drawn: LEVEL_WORDS[level.kind].drawn,
    instrument: instrumentOf(level.contract, known),
    attempt: attemptWords(level.entry_number),
  }));
}

export function fillRows(session: SessionDetail): FillRow[] {
  const known = knownContracts(session);
  return session.fills.map((fill, place) => ({
    key: `${place}:${fill.contract}:${fill.leg_id}:${fill.entry_number}:${fill.kind}`,
    fill,
    name: FILL_WORDS[fill.kind].name,
    drawn: FILL_WORDS[fill.kind].drawn,
    instrument: instrumentOf(fill.contract, known),
    attempt: attemptWords(fill.entry_number),
  }));
}

export function markRows(session: SessionDetail): MarkRow[] {
  const known = knownContracts(session);
  return session.marks.map((mark, place) => ({
    key: `${place}:${mark.contract}:${mark.leg_id}:${mark.entry_number}`,
    mark,
    name: MARK_WORDS.name,
    drawn: MARK_WORDS.drawn,
    instrument: instrumentOf(mark.contract, known),
    attempt: attemptWords(mark.entry_number),
  }));
}
