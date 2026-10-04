import { afterRenderEffect, linkedSignal, type ElementRef, type Signal, type WritableSignal } from '@angular/core';

import { isWeekend, parseIsoDate, type IsoDate } from '../date/iso-date';
import type { DateBounds } from './date-bounds';
import type { TradingSessions } from './trading-sessions';

/** What a typed date is checked against. */
export interface DateCheckContext {
  readonly bounds: DateBounds;
  /** The calendar, when the date must be a trading session; null leaves the day unchecked. */
  readonly sessions: TradingSessions | null;
}

/** The date a field's text stands for, or why it stands for none. Empty text is empty, not a problem. */
export interface DateCheck {
  /** The usable date; empty when the text is empty or refused. */
  readonly date: IsoDate | '';
  readonly problem: string | null;
}

const USABLE = (date: IsoDate): DateCheck => ({ date, problem: null });
const REFUSED = (problem: string): DateCheck => ({ date: '', problem });

function closedDay(date: IsoDate, sessions: TradingSessions): string {
  const holiday = sessions.holidayName(date);
  const why = holiday !== null ? `a market holiday (${holiday})` : isWeekend(date) ? 'a weekend' : 'not a trading day';
  const next = sessions.onOrAfter(date);
  return `${date} is ${why}.${next === null ? '' : ` The next session is ${next}.`}`;
}

/**
 * Reads one typed date. Nothing is corrected: text that is not a whole
 * ``YYYY-MM-DD`` date, falls outside the bounds, or names a closed day is
 * refused with the reason, and the caller hands its host an empty date.
 */
export function checkDate(text: string, context: DateCheckContext): DateCheck {
  if (text === '') return { date: '', problem: null };
  const date = parseIsoDate(text);
  if (date === null) return REFUSED('Enter a date as YYYY-MM-DD.');
  const { bounds, sessions } = context;
  if (bounds.min !== null && date < bounds.min) return REFUSED(bounds.minReason ?? `The first day is ${bounds.min}.`);
  if (bounds.max !== null && date > bounds.max) return REFUSED(bounds.maxReason ?? `The last day is ${bounds.max}.`);
  if (sessions !== null) {
    if (!sessions.covers(date)) return REFUSED(`The trading calendar runs from ${sessions.from} to ${sessions.through}.`);
    if (!sessions.isSession(date)) return REFUSED(closedDay(date, sessions));
  }
  return USABLE(date);
}

/** Reads a typed range: each end on its own, then their order. */
export function checkRange(fromText: string, toText: string, context: DateCheckContext): { from: DateCheck; to: DateCheck } {
  const from = checkDate(fromText, context);
  const to = checkDate(toText, context);
  if (from.date !== '' && to.date !== '' && from.date > to.date) {
    return { from: REFUSED('The start date is after the end date.'), to };
  }
  return { from, to };
}

/**
 * The text of a date field, which is not always the date its host holds: an
 * unusable entry stays in the field while the host is handed an empty date.
 *
 * The text follows the host's date, except when that date is the one the
 * field itself just handed over. Then the host is echoing the field back, and
 * the text the person typed stays.
 */
export function fieldText(hostDate: Signal<string>, lastHanded: () => string | null): WritableSignal<string> {
  return linkedSignal<string, string>({
    source: hostDate,
    computation: (date, previous) => (previous !== undefined && date === lastHanded() ? previous.value : date),
  });
}

/**
 * Keeps a date field's input showing its text (#2866). A template binding
 * writes the input only when the text differs from what the binding last
 * wrote, and what a person types never passes through it: text that changes
 * and is put back within one task would leave their typing on screen.
 */
export function showFieldText(input: Signal<ElementRef<HTMLInputElement>>, text: Signal<string>): void {
  afterRenderEffect({
    write: () => {
      const shown = text();
      const element = input().nativeElement;
      if (element.value !== shown) element.value = shown;
    },
  });
}
