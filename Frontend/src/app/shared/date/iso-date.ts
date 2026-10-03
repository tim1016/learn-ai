import { isCalendarDate } from './local-wall-clock';

/** A calendar date as ``YYYY-MM-DD``. Text order is date order, so dates compare with ``<`` and ``>``. */
export type IsoDate = string;

const MS_PER_DAY = 24 * 60 * 60 * 1000;

function parts(date: IsoDate): [number, number, number] {
  const [year, month, day] = date.split('-').map(Number);
  return [year, month, day];
}

/** ``text`` when it names a real calendar day with a whole four-digit year, else null. */
export function parseIsoDate(text: string): IsoDate | null {
  return isCalendarDate(text) && text >= '1000' ? text : null;
}

/** ``date`` moved by whole calendar days (no zone involved). */
export function addDays(date: IsoDate, days: number): IsoDate {
  const [year, month, day] = parts(date);
  return new Date(Date.UTC(year, month - 1, day) + days * MS_PER_DAY).toISOString().slice(0, 10);
}

/** Whether ``date`` falls on a Saturday or Sunday. */
export function isWeekend(date: IsoDate): boolean {
  const [year, month, day] = parts(date);
  const weekday = new Date(Date.UTC(year, month - 1, day)).getUTCDay();
  return weekday === 0 || weekday === 6;
}

/**
 * The viewer-local midnight ``Date`` a calendar widget works in.
 *
 * Display-side only: the widget draws a grid of days and hands a ``Date``
 * back, which {@link fromCalendarDate} reads by its local fields. Nothing
 * stores or sends this value, so the viewer's zone never reaches a date.
 */
export function toCalendarDate(date: IsoDate): Date {
  const [year, month, day] = parts(date);
  return new Date(year, month - 1, day);
}

/** The calendar day a widget's local ``Date`` stands for. */
export function fromCalendarDate(date: Date): IsoDate {
  const two = (value: number): string => String(value).padStart(2, '0');
  return `${date.getFullYear()}-${two(date.getMonth() + 1)}-${two(date.getDate())}`;
}
