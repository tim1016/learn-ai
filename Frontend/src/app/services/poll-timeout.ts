import { firstValueFrom, type Observable, timeout } from 'rxjs';

/**
 * Ceiling for a polled read's HTTP request.
 *
 * Angular's `HttpClient` never times out on its own. A polled resource
 * guards its interval on `isLoading()`, so one request that never settles
 * skips every subsequent tick — the roster stayed frozen for 9+ minutes
 * across a data-plane restart while its footer kept looking fresh (S7).
 * A finite ceiling turns that hang into a rejection the existing error and
 * staleness affordances already handle, and the next tick recovers
 * unattended.
 *
 * Deliberately longer than the 5s catalog interval: each poll is guarded on
 * `isLoading()`, so a request that outlives its interval skips ticks rather
 * than stacking, and a ceiling below the interval would cut off a
 * slow-but-live backend. The ceiling's job is to bound the freeze to one
 * request, not to fit inside a tick.
 */
export const POLL_REQUEST_TIMEOUT_MS = 15_000;

/** Await `source`, rejecting if it has not emitted within the poll ceiling. */
export function firstValueWithinPollTimeout<T>(source: Observable<T>): Promise<T> {
  return firstValueFrom(source.pipe(timeout({ first: POLL_REQUEST_TIMEOUT_MS })));
}
