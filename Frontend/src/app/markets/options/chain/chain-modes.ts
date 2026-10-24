/*
 * The four ways to ask for a chain (PRD #3490 R11), declared once. The strip
 * and the chain view's Mode fact both read this list, and no source adapter
 * writes its own mode's name, so a mode can never pick up a second name or a
 * second reason.
 *
 * The reasons are the domain's own words. A mode that cannot be chosen stays
 * on the strip, disabled, with its reason beside it, and is never replaced by
 * another mode's chain.
 */

export type ChainModeId = 'now-snapshot' | 'now-live' | 'past-archive' | 'past-live';

/** The values `?source=` takes. Both Live IBKR modes are `live`: the time, not the source, tells them apart. */
export type ChainSource = 'snapshot' | 'live' | 'archive';

export interface ChainMode {
  readonly id: ChainModeId;
  readonly when: 'Now' | 'Past';
  readonly label: string;
  readonly source: ChainSource;
  /** Why this mode cannot be chosen, or `null` when it can. */
  readonly unavailable: string | null;
}

/** Live IBKR answers for the present only: a past instant is never read from today's feed. */
export const PAST_LIVE_REASON =
  'Past · Live IBKR is not available. IBKR streams the present only and cannot answer for a past instant, so it never stands in for the archive and never shows today\'s chain under a past date.';

export const CHAIN_MODES: readonly ChainMode[] = [
  { id: 'now-snapshot', when: 'Now', label: 'Snapshot', source: 'snapshot', unavailable: null },
  { id: 'now-live', when: 'Now', label: 'Live IBKR', source: 'live', unavailable: null },
  { id: 'past-archive', when: 'Past', label: 'Archive', source: 'archive', unavailable: null },
  { id: 'past-live', when: 'Past', label: 'Live IBKR', source: 'live', unavailable: PAST_LIVE_REASON },
];

/** A mode's name as every surface writes it: `Now · Snapshot`. */
export function chainModeName(id: ChainModeId): string {
  const mode = CHAIN_MODES.find((candidate) => candidate.id === id);
  return mode === undefined ? id : `${mode.when} · ${mode.label}`;
}
