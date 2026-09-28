/** The bot hues, in palette order. Colour is never the only carrier: every
 * surface that draws a bot in its hue also names it. */
export const BOT_HUES = [
  'var(--chart-series-blue)',
  'var(--chart-series-amber)',
  'var(--chart-series-pink)',
  'var(--chart-series-green)',
  'var(--chart-series-purple)',
  'var(--chart-series-orange)',
] as const;

/** The part of a money-bar segment a hue is chosen from. */
export interface HuedSegment {
  readonly kind: string;
  readonly palette_index?: number | null;
}

/**
 * The hue of each running bot's slice, in segment order, and `null` for every
 * other kind (their treatment is their own pattern).
 *
 * A bot's hue is its backend `palette_index` — its place in the account's
 * registration order — wrapped onto the palette, so it keeps one colour on
 * every bar it appears in however other bots come and go. A segment without
 * one falls back to its place among this bar's bot slices: stable within one
 * bar, not across bars.
 *
 * This is palette arithmetic, not money: it lives outside the money surfaces
 * so their lint rules stay absolute.
 */
export function botHues(segments: readonly HuedSegment[]): readonly (string | null)[] {
  let ordinal = 0;
  return segments.map((segment) => {
    if (segment.kind !== 'bot') return null;
    const slot = segment.palette_index ?? ordinal;
    ordinal += 1;
    return BOT_HUES[slot % BOT_HUES.length];
  });
}
