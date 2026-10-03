import type { components } from '../../api/broker.types';
import { etIsoDate } from '../date/et-midnight';
import type { IsoDate } from '../date/iso-date';

/** The wire shape of ``GET /api/calendar/sessions``. */
export type TradingCalendarWindow = components['schemas']['TradingCalendarWindow'];

/**
 * The scheduled NYSE sessions between two dates, as the calendar dates a
 * picker works in.
 *
 * Built once from the data plane's canonical calendar; the browser holds no
 * calendar of its own. Every question is answered only inside
 * ``[from, through]``, the window that was read: ask {@link covers} first.
 */
export class TradingSessions {
  private readonly days: readonly IsoDate[];
  private readonly closes: readonly number[];
  private readonly position: ReadonlyMap<IsoDate, number>;
  private readonly holidays: ReadonlyMap<IsoDate, string>;

  constructor(
    window: TradingCalendarWindow,
    readonly from: IsoDate,
    readonly through: IsoDate,
  ) {
    this.days = window.session_open_ms.map(etIsoDate);
    this.closes = window.session_close_ms;
    this.position = new Map(this.days.map((day, index) => [day, index]));
    this.holidays = new Map(window.holidays.map((holiday) => [etIsoDate(holiday.date_ms), holiday.name]));
  }

  /** Whether the calendar was read for ``date`` at all. */
  covers(date: IsoDate): boolean {
    return this.from <= date && date <= this.through;
  }

  isSession(date: IsoDate): boolean {
    return this.position.has(date);
  }

  /** The exchange's name for the closure on ``date``; null when it names none. */
  holidayName(date: IsoDate): string | null {
    return this.holidays.get(date) ?? null;
  }

  /** The first session on or after ``date``; null when the window holds none. */
  onOrAfter(date: IsoDate): IsoDate | null {
    return this.days[this.firstIndexAtOrAfter(date)] ?? null;
  }

  /** The last session on or before ``date``; null when the window holds none. */
  onOrBefore(date: IsoDate): IsoDate | null {
    const exact = this.position.get(date);
    if (exact !== undefined) return date;
    return this.days[this.firstIndexAtOrAfter(date) - 1] ?? null;
  }

  /** How many sessions fall in ``[from, to]``, both ends included. */
  count(from: IsoDate, to: IsoDate): number {
    if (to < from) return 0;
    const end = this.position.get(to) ?? this.firstIndexAtOrAfter(to) - 1;
    return Math.max(0, end - this.firstIndexAtOrAfter(from) + 1);
  }

  /** The last session whose scheduled close is at or before ``nowMs``; null when none has closed. */
  lastCompleted(nowMs: number): IsoDate | null {
    return this.days[this.closedBy(nowMs) - 1] ?? null;
  }

  /** The first scheduled close after ``nowMs``; null when the window holds none. */
  nextCloseAfter(nowMs: number): number | null {
    return this.closes[this.closedBy(nowMs)] ?? null;
  }

  /** Every weekday the exchange is closed in ``[from, to]``, oldest first. */
  holidaysBetween(from: IsoDate, to: IsoDate): IsoDate[] {
    return [...this.holidays.keys()].filter((day) => from <= day && day <= to).sort();
  }

  /** How many sessions have closed at or before ``nowMs``. */
  private closedBy(nowMs: number): number {
    let low = 0;
    let high = this.closes.length;
    while (low < high) {
      const middle = (low + high) >> 1;
      if (this.closes[middle] <= nowMs) low = middle + 1;
      else high = middle;
    }
    return low;
  }

  private firstIndexAtOrAfter(date: IsoDate): number {
    let low = 0;
    let high = this.days.length;
    while (low < high) {
      const middle = (low + high) >> 1;
      if (this.days[middle] < date) low = middle + 1;
      else high = middle;
    }
    return low;
  }
}
