import type { ExpiryTag } from './archive/past-chain.types';
import type { ChainModeId } from './chain-modes';

/*
 * What the one chain presentation draws (PRD #3490 R11). Every source adapter
 * maps its own wire into this and nothing else reaches the grid, so a source
 * can only show what its type can carry: a last-trade price is a `last`
 * field and never a `bid` or `ask`, and a Greek or an open interest is only
 * ever a field the adapter chose to fill.
 *
 * A field is its drawn text, or the reason it is absent. There is no blank, no
 * zero and no value borrowed from another source: a source that does not hold
 * a field says so in its `absent` reason.
 */

/** Why a value is not on a row, as a code a spec can name. `chainAbsentWords` says each in words. */
export type ChainAbsentReason =
  /** The contract is listed and its prices are not stored. */
  | 'not_stored'
  /** The contract is listed and had not traded by the instant. */
  | 'did_not_trade'
  /** Stored, and cannot be read. */
  | 'unreadable'
  /** The answer carried a value observed after the instant it is read at. It is never drawn. */
  | 'after_instant'
  /** The answer said it traded and left out the price of the trade. */
  | 'no_price_sent'
  /** The answer said it traded and left out the time of the trade, and a price is never drawn without its time. */
  | 'no_time_sent'
  /** No bar of the underlying had closed by the instant. */
  | 'no_bar_yet'
  /** The underlying's bars are not stored. */
  | 'bars_not_stored'
  /** The underlying's bars are stored and cannot be read. */
  | 'bars_unreadable'
  /** A state the page does not know. */
  | 'unknown_state'
  /** The source holds trade bars, not quotes. */
  | 'no_quote_archived'
  /** No snapshot was recorded for that date, so no Greek, open interest or volume exists for it. */
  | 'no_recorded_snapshot'
  /** The provider's snapshot carried nothing for this field. */
  | 'not_in_snapshot';

export type ChainField =
  | {
      readonly kind: 'value';
      /** Drawn exactly as the adapter wrote it. */
      readonly text: string;
      /** When the value was observed (`int64 ms UTC`), for a source that dates each value; else null. */
      readonly atMs: number | null;
      /** A share of the busiest cell, 0 to 100, for a column that draws a bar behind its figure. */
      readonly bar?: number;
    }
  | {
      readonly kind: 'absent';
      readonly reason: ChainAbsentReason;
      /** The source's own words about it (an unreadable file's reason, the code of an unknown state); else null. */
      readonly detail: string | null;
    };

/** The columns a side can draw, in the words the grid heads them with. */
export type ChainColumnId = 'delta' | 'gamma' | 'theta' | 'vega' | 'last' | 'bid' | 'ask' | 'spread' | 'volume' | 'openInterest';

export const CHAIN_COLUMN_LABELS: Readonly<Record<ChainColumnId, string>> = {
  delta: 'Δ',
  gamma: 'Γ',
  theta: 'Θ',
  vega: 'V',
  last: 'Last',
  bid: 'Bid',
  ask: 'Ask',
  spread: 'Sprd',
  volume: 'Volume',
  openInterest: 'OI',
};

/**
 * The columns each side draws, left to right as they stand on screen: the calls
 * run outward to inward, the puts inward to outward. `full` is a source that dates each
 * value and says why one is absent in words (one wide price column); `compact` is a source
 * that fills many narrow columns, where an absent value is a dash with its reason on hover.
 */
export interface ChainColumns {
  readonly calls: readonly ChainColumnId[];
  readonly puts: readonly ChainColumnId[];
  readonly detail: 'compact' | 'full';
}

/** One contract on one side of a strike: who it is, and the fields its source holds for it. */
export interface ChainContractCell {
  readonly kind: 'contract';
  /** The contract's symbol as its source named it. */
  readonly symbol: string;
  /** A standard contract of a hundred shares; null when the source does not say. */
  readonly standard: boolean | null;
  /**
   * Every field a contract can have, each one filled or absent with its reason. The record is
   * complete on purpose: a source cannot leave a Greek, an open interest or a quote unsaid, so a
   * source that holds none of them says why, and nothing else's value fills the place.
   */
  readonly fields: Readonly<Record<ChainColumnId, ChainField>>;
}

/** No contract on this side of the strike, and the words for why that is not a missing price. */
export interface ChainGapCell {
  readonly kind: 'gap';
  readonly words: string;
}

export type ChainSideCell = ChainContractCell | ChainGapCell;

export interface ChainRow {
  /** Unique within the table. */
  readonly key: string;
  /** The strike, drawn exactly as the adapter wrote it. */
  readonly strike: string;
  /** This row holds the money: the grid marks it, with `mark` as the words of the badge. */
  readonly atm: boolean;
  readonly mark: string | null;
  /** A side that is in the money for the spot the adapter knows; null where the source has no spot to say it by. */
  readonly itm: 'call' | 'put' | null;
  /** The figure between the sides (implied volatility), when the table has one. */
  readonly centre: ChainField | null;
  readonly call: ChainSideCell;
  readonly put: ChainSideCell;
}

export interface ChainTable {
  readonly columns: ChainColumns;
  /** Whether the strike's neighbour column (implied volatility) is drawn. */
  readonly centre: boolean;
  readonly rows: readonly ChainRow[];
  /** What the table is, for the reader who reaches it without its heading. */
  readonly caption: string;
}

export type ChainFactValue =
  | { readonly kind: 'text'; readonly text: string }
  /** An instant in the viewer's own time (`local`) or New York's (`et`). */
  | { readonly kind: 'instant'; readonly ms: number; readonly mode: 'local' | 'et'; readonly granularity: 'time' | 'minute' }
  /** A date-anchored instant, read in New York and never shifted a day. */
  | { readonly kind: 'date'; readonly ms: number }
  /** A price with the time it was observed, or why there is none. */
  | { readonly kind: 'field'; readonly field: ChainField }
  | { readonly kind: 'expiry'; readonly ms: number; readonly tag: ExpiryTag | null };

export interface ChainFact {
  readonly label: string;
  readonly value: ChainFactValue;
  /** `warn` and `bad` are for a state the reader should not miss (delayed, stale); the words carry it as well. */
  readonly tone?: 'warn' | 'bad';
}

/** What one source says for one underlying and one expiry. */
export interface ChainPresentation {
  readonly mode: ChainModeId;
  /** Contract identity: the underlying and expiry this chain is of. They survive filtering, and a mode change leaves them in the address. */
  readonly underlying: string;
  readonly expiryMs: number;
  readonly facts: readonly ChainFact[];
  /** What this source cannot say, as sentences under the facts. */
  readonly notes: readonly string[];
  readonly table: ChainTable;
}

const ABSENT_WORDS: Readonly<Record<ChainAbsentReason, string>> = {
  not_stored: 'Listed; prices not stored',
  did_not_trade: 'Did not trade by then',
  unreadable: 'Prices cannot be read',
  after_instant: 'Observed after this instant, so not shown',
  no_price_sent: 'Traded, but the source sent no price for the trade, so none is shown',
  no_time_sent: 'Traded, but the source sent no time for the trade, so no price is shown',
  no_bar_yet: 'No bar has closed by then',
  bars_not_stored: 'Its bars are not stored',
  bars_unreadable: 'Its bars cannot be read',
  unknown_state: 'This page does not know this state of its price',
  no_quote_archived: 'The archive holds trade bars, not quotes',
  no_recorded_snapshot: 'No snapshot was recorded for this date',
  not_in_snapshot: 'The provider sent none',
};

/** The words for an absent field, with the source's own detail after them when it gave one. */
export function chainAbsentWords(field: Extract<ChainField, { kind: 'absent' }>): string {
  const words = ABSENT_WORDS[field.reason];
  return field.detail === null ? words : `${words}: ${field.detail}`;
}

/** The mark of a field that is absent because its source has none of it at all. */
export function absent(reason: ChainAbsentReason, detail: string | null = null): Extract<ChainField, { kind: 'absent' }> {
  return { kind: 'absent', reason, detail };
}

/**
 * Why the L / S buttons are disabled. A chain contract fixes strike and
 * expiry but carries no entry, exit or stop timing, so it cannot become a
 * runnable setup without inventing rules (PRD #3490, D04).
 */
export const ADD_LEG_UNAVAILABLE_REASON = "Add leg can't yet carry timing rules into a setup.";
