import type { OptionContract } from '../../../shared/option-contract/option-contract.component';
import type { LevelKind, SessionFill, TradeContract } from './run-detail.types';

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

/** What a fill is: a purchase or a sale. */
export const FILL_WORDS: Readonly<Record<SessionFill['kind'], { readonly name: string; readonly drawn: string }>> = {
  entry: { name: 'Bought', drawn: 'up arrow' },
  exit: { name: 'Sold', drawn: 'down arrow' },
};

/** A leg valued at the end and not sold: not a fill, and told apart from one everywhere. */
export const MARK_WORDS = { name: 'Marked, not sold', drawn: 'square' } as const;
