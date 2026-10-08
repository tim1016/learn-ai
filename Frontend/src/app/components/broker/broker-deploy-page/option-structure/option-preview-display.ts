import type { components } from '../../../../api/broker.types';

type OptionPreviewQuote = components['schemas']['OptionPreviewQuote'];

/** An age the server sends in milliseconds, in whole seconds. */
export function ageSeconds(ageMs: number): number {
  return Math.max(0, Math.floor(ageMs / 1000));
}

/** A two-sided quote is as old as its older side. */
export function quoteAgeSeconds(quote: OptionPreviewQuote): number {
  return ageSeconds(Math.max(quote.bid_age_ms, quote.ask_age_ms));
}

/** A delta to two places for the eye, or `null` when there is none. */
export function deltaText(delta: number | null): string | null {
  return delta === null ? null : delta.toFixed(2);
}
