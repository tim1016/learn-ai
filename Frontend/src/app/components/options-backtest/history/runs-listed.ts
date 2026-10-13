import { InjectionToken } from '@angular/core';

/** How many runs the History list asks for, and the most it may ask for. */
export interface RunsListed {
  /** The runs asked for first, and how many more each "Show older runs" asks for. The route's own default is 200. */
  readonly page: number;
  /** The most the server lists: its route takes a `limit` of 1000 at the most, and refuses a larger one. */
  readonly most: number;
}

/** A spec that is about the list being cut short sets small numbers, so that it draws a handful of rows and not a thousand. */
export const RUNS_LISTED = new InjectionToken<RunsListed>('RUNS_LISTED', { providedIn: 'root', factory: () => ({ page: 200, most: 1000 }) });
