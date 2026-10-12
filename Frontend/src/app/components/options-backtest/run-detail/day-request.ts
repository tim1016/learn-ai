import { formatTimestampDisplay } from '../../../shared/timestamp';

/**
 * An owner's wish to look at one day of a run: the session's open, and the control that asked, so that closing the day
 * gives the keyboard back to it. A new object is made for each press, even for the same day, so asking again is heard.
 */
export interface DayRequest {
  readonly openMs: number;
  readonly origin: HTMLElement | null;
}

/** The request a press of a control makes: the day, and the control that was pressed (null when the event names none). */
export function dayRequestFrom(openMs: number, event: Event): DayRequest {
  return { openMs, origin: event.currentTarget instanceof HTMLElement ? event.currentTarget : null };
}

/** What a control that opens a day is called, by the day's New York date: "Look at 2026-09-15". */
export function lookAtLabel(openMs: number): string {
  return `Look at ${formatTimestampDisplay(openMs, { mode: 'date-et', granularity: 'date' })}`;
}
