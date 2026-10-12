import { InjectionToken } from '@angular/core';

/**
 * How often, in milliseconds, the page reads a run again while its record says it is going, and looks again for a
 * record it has not found. The job's stream says how it is going, but the record is the truth, and a stream can be
 * missing or silent. A spec that is not about the poll sets 0, which switches it off.
 */
export const RUN_POLL_MS = new InjectionToken<number>('RUN_POLL_MS', { providedIn: 'root', factory: () => 3000 });
