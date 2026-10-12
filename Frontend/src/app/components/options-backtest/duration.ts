import { etDayEndMs, etIsoDate, etMidnightMs } from '../../shared/date/et-midnight';
import { UNBOUNDED, type DateBounds, type DateRange, type QuickRangeKey } from '../../shared/date-picker';
import { formatTimestampDisplay } from '../../shared/timestamp';
import type { TestableStretch } from './options-backtest.types';

/**
 * The dates of a run. They are not part of the setup: the same setup is
 * replayed over whatever dates are asked for, so they are kept apart from the
 * draft, the saved setup and the exported file.
 */

/** No dates yet. The picker's dates are `YYYY-MM-DD`, New York calendar dates, the last one included; `''` while an end holds no usable date. */
export const NO_DATES: DateRange = { from: '', to: '' };

/** The quick ranges the page offers: a run reaches back only as far as the options plan does, so no 5Y and no Max. */
export const OFFERED_RANGES: readonly QuickRangeKey[] = ['1W', '1M', '3M', '6M', 'YTD', '1Y', '2Y'];

/** The half-open range `[start_ms, end_ms)` a run takes: New York midnight of the first date, to New York midnight after the last. */
export interface RangeInstants {
  readonly start_ms: number;
  readonly end_ms: number;
}

/** The two instants for two dates, made with the helpers Grid Search builds its window with; null while an end holds no date. */
export function rangeInstants(range: DateRange): RangeInstants | null {
  if (range.from === '' || range.to === '') return null;
  return { start_ms: etMidnightMs(range.from), end_ms: etDayEndMs(range.to) };
}

/** The picker's bounds: nothing before the day option prices are available from, once the server has said it. */
export function priceBounds(optionPricesFromMs: number | null): DateBounds {
  if (optionPricesFromMs === null) return UNBOUNDED;
  const first = formatTimestampDisplay(optionPricesFromMs, { mode: 'date-et', granularity: 'date' });
  return { ...UNBOUNDED, min: etIsoDate(optionPricesFromMs), minReason: `Option prices are available from ${first}, so no earlier day can be picked.` };
}

/** The dates of the longest stretch of sessions that can all be tested, as the picker holds them. */
export function rangeOfStretch(stretch: TestableStretch): DateRange {
  return { from: etIsoDate(stretch.first_session_open_ms), to: etIsoDate(stretch.last_session_open_ms) };
}

/** A range the page set on the owner's click, in the sessions' own terms, for the line that says so. */
export interface RangeChange {
  readonly firstSessionOpenMs: number;
  readonly lastSessionOpenMs: number;
}
