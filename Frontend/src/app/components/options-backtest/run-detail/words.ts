import type { OptionContract } from '../../../shared/option-contract/option-contract.component';
import type { LegTrade, LevelKind, SessionDetail, SessionFill, SessionMark, TradeContract } from './run-detail.types';

/**
 * A contract as the shared renderer takes it, made from the fields the server sent (its OCC symbol, underlying, right,
 * strike and expiry). The page never reads a symbol to find these.
 */
export function contractOf(contract: TradeContract): OptionContract {
  return {
    occ_symbol: contract.symbol,
    underlying: contract.underlying,
    expiry_ms: contract.expiry_ms,
    right: contract.right,
    strike: contract.strike_usd,
  };
}

/**
 * The contracts of a day by OCC symbol, with the parts the server sent for each (from the day's trades and the series it
 * charts). Events, levels, fills and marks name a contract by its symbol alone, and this is how they are read in words
 * without the page ever taking a symbol apart.
 */
export function knownContracts(session: SessionDetail): ReadonlyMap<string, TradeContract> {
  const known = new Map<string, TradeContract>();
  for (const trade of session.trades) known.set(trade.contract.symbol, trade.contract);
  for (const series of session.contracts) if (series.contract != null) known.set(series.contract.symbol, series.contract);
  return known;
}

/**
 * Which try a leg is on, in words: the entry number the server counts from 0 is the leg's first entry, and each later
 * one a re-entry with the server's own number. The page works out nothing to say it.
 */
export function attemptWords(entryNumber: number): string {
  return entryNumber === 0 ? 'first entry' : `re-entry ${entryNumber}`;
}

/** A leg and the try it is on: "call, first entry". */
export function legAttempt(legId: string, entryNumber: number): string {
  return `${legId}, ${attemptWords(entryNumber)}`;
}

/** What each line on the chart is, in words, with the way it is drawn so that colour is never the only difference. */
export const LEVEL_WORDS: Readonly<Record<LevelKind, { readonly name: string; readonly drawn: string }>> = {
  starting_price: { name: 'Starting price', drawn: 'dotted line' },
  momentum: { name: 'Momentum level', drawn: 'dashed line' },
  stop: { name: 'Stop level', drawn: 'long dashes' },
};

/** What a fill of a bought leg is: a purchase to open, then a sale to close. The arrow points the way it traded. */
export const FILL_WORDS: Readonly<Record<SessionFill['kind'], { readonly name: string; readonly drawn: string }>> = {
  entry: { name: 'Bought', drawn: 'up arrow' },
  exit: { name: 'Sold', drawn: 'down arrow' },
};

/** What a fill of a sold leg is: a sale to open, then a buy-back to close. A sale points down and a buy-back up (#3109). */
export const SOLD_FILL_WORDS: Readonly<Record<SessionFill['kind'], { readonly name: string; readonly drawn: string }>> = {
  entry: { name: 'Sold to open', drawn: 'down arrow' },
  exit: { name: 'Bought back', drawn: 'up arrow' },
};

/**
 * A leg valued at the end and not sold: not a fill, and told apart from one everywhere. `inline` is how a sentence about
 * the trade says it.
 */
export const MARK_WORDS = { name: 'Marked, not sold', drawn: 'square', inline: 'not sold' } as const;

/** A sold leg valued at the end and not bought back: an amount it owes and not a value it holds (#3109). */
export const OWED_MARK_WORDS = { name: 'Marked, owed', drawn: 'square', inline: 'owed, not bought back' } as const;

/** The two things a fill says of itself: whether it opened or closed its leg, and which way it traded. */
type FillOf = Pick<SessionFill, 'kind' | 'side'>;

/**
 * Whether a fill is of a sold leg: it opened by a sale or closed by a buy-back. A fill says which way it traded and the
 * page never asks the leg's own side, so a leg and its fills cannot be read as two different things.
 */
export function isSoldLegFill(fill: FillOf): boolean {
  return (fill.kind === 'entry') === (fill.side === 'sell');
}

/** A fill in words, by the side it traded on and whether it opened or closed its leg. */
export function fillWords(fill: FillOf): { readonly name: string; readonly drawn: string } {
  return (isSoldLegFill(fill) ? SOLD_FILL_WORDS : FILL_WORDS)[fill.kind];
}

/** A mark in words: a bought leg's is not sold and a sold leg's is owed. */
export function markWords(mark: Pick<SessionMark, 'owed'>): typeof MARK_WORDS | typeof OWED_MARK_WORDS {
  return mark.owed ? OWED_MARK_WORDS : MARK_WORDS;
}

/** Whether a trade is of a sold leg: its entry was a sale, so a close of it is a buy-back and a mark of it is owed. */
export function isSoldLegTrade(trade: Pick<LegTrade, 'entry'>): boolean {
  return trade.entry.side === 'sell';
}

/** The heading of the column that says why a leg was closed. It is "Sold because" until a buy-back is among the fills. */
export function closedBecauseHeading(fills: readonly FillOf[]): string {
  return fills.some(isSoldLegFill) ? 'Closed because' : 'Sold because';
}

/** What the section of marks is, and its caption: a mark of a sold leg is owed, and a day may hold both kinds. */
export function marksSection(marks: readonly Pick<SessionMark, 'owed'>[]): { readonly heading: string; readonly caption: string } {
  const owed = marks.filter((mark) => mark.owed).length;
  if (owed === 0) return { heading: MARK_WORDS.name, caption: 'Legs marked at the last price their contract traded at, and not sold' };
  const caption = 'Legs marked at the last price their contract traded at, and not closed';
  if (owed === marks.length) return { heading: OWED_MARK_WORDS.name, caption: `${caption}: each was sold and not bought back, so it is owed` };
  return { heading: 'Marked, not sold or owed', caption: `${caption}: a bought leg is not sold and a sold leg is owed` };
}
