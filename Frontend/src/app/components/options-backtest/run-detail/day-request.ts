/**
 * An owner's wish to look at one day of a run: the session's open, and the control that asked, so that closing the day
 * gives the keyboard back to it. A new object is made for each press, even for the same day, so asking again is heard.
 */
export interface DayRequest {
  readonly openMs: number;
  readonly origin: HTMLElement | null;
}
