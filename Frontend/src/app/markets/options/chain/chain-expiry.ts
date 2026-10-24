import { etIsoDate } from '../../../shared/date/et-midnight';
import { optionExpiryAnchorMs } from '../../../shared/option-contract/option-expiry-date';

/**
 * The nearest expiry a source lists that has not passed: the first anchor at or after today's
 * New York date, else the first listed. `null` where it lists none. A source that opens a chain
 * for an address that names no expiry opens this one, and says so.
 */
export function nearestExpiry(listed: readonly number[], nowMs: number): number | null {
  const today = optionExpiryAnchorMs(etIsoDate(nowMs));
  return listed.find((expiry) => expiry >= today) ?? listed[0] ?? null;
}
