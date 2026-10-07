import type { components } from '../../../../api/broker.types';

type OptionPreviewQuote = components['schemas']['OptionPreviewQuote'];

const THOUSANDS = /\B(?=(\d{3})+(?!\d))/g;

/**
 * Whole cents as dollars, digit for digit: `$1,234.56`, `-$0.37`. The server
 * sends an option's money as whole cents; this only writes them out, with
 * integer arithmetic, and works nothing out.
 */
export function usdFromCents(cents: number): string {
  const size = Math.abs(cents);
  const dollars = String(Math.trunc(size / 100)).replace(THOUSANDS, ',');
  return `${cents < 0 ? '-' : ''}$${dollars}.${String(size % 100).padStart(2, '0')}`;
}

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
