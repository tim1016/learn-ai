import type { IsoDate } from '../date/iso-date';

/**
 * The days a page's run can read, as its date picker enforces them.
 *
 * The host states the bound, because only the host knows what its run reads:
 * a lake-reading page passes its instrument's held span, a page whose run
 * fetches for itself passes the provider's history floor.
 */
export interface DateBounds {
  /** First pickable day; null for no lower bound. */
  readonly min: IsoDate | null;
  /** Last pickable day; null to end on the last completed session. */
  readonly max: IsoDate | null;
  /** Why nothing before ``min`` can be picked. */
  readonly minReason: string | null;
  /** Why nothing after ``max`` can be picked. */
  readonly maxReason: string | null;
  /**
   * Why the span is not known. Quick ranges cannot be offered without one;
   * typed dates are left unchecked, so a page's starting dates survive
   * until its instrument's span has been read.
   */
  readonly unknown: string | null;
}

export const UNBOUNDED: DateBounds = { min: null, max: null, minReason: null, maxReason: null, unknown: null };

/** The bound of a page that reads the lake for one instrument: the days the lake holds. */
export function heldBounds(symbol: string, firstHeld: IsoDate | null, lastHeld: IsoDate | null): DateBounds {
  if (symbol === '') return { ...UNBOUNDED, unknown: 'Pick an instrument to see the dates it is held for.' };
  if (lastHeld === null) return { ...UNBOUNDED, unknown: `${symbol} has no held days to pick from.` };
  return {
    min: firstHeld,
    max: lastHeld,
    minReason: firstHeld === null ? null : `${symbol} is held from ${firstHeld}.`,
    maxReason: `${symbol} is held through ${lastHeld}.`,
    unknown: null,
  };
}

/**
 * What a host knows about the sessions its data lacks inside the bounds.
 *
 * A held span is only a first and a last day: sessions between them can be
 * missing. A host whose run reads the lake as it stands hands the picker
 * these, and the picker marks them in the calendar and counts them under the
 * dates. The range stays selectable; the host's run decides what a gap means.
 */
export interface DataGaps {
  /** The sessions inside the bounds that the host's data does not hold. */
  readonly unheld: ReadonlySet<IsoDate>;
  /** Why the gaps could not be read; null when they were. */
  readonly problem: string | null;
}
