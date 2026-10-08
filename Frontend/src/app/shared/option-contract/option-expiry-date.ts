import { etDayEndMs, etIsoDate } from '../date/et-midnight';
import type { IsoDate } from '../date/iso-date';

const EIGHT_HOURS_MS = 8 * 60 * 60 * 1000;

/**
 * An option expiry date as the one instant that names it: 16:00 New York
 * time on that date, `int64 ms UTC` (ADR 0022 (i)). It is what a route that
 * takes an expiry takes; the server refuses any other instant.
 *
 * New York's clocks change at 02:00, so 16:00 is always eight hours before
 * the next midnight there, whatever the date.
 */
export function optionExpiryAnchorMs(date: IsoDate): number {
  return etDayEndMs(date) - EIGHT_HOURS_MS;
}

/** The expiry date an anchor names, read in New York. */
export function optionExpiryDate(anchorMs: number): IsoDate {
  return etIsoDate(anchorMs);
}
