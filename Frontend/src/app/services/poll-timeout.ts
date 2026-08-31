/**
 * Ceiling for a polled read, from the moment it is scheduled.
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
 *
 * `PolledReadScheduler` spends this budget from the moment a read is
 * scheduled rather than from the moment it is issued (#1912). Reads now
 * queue behind one another, and time spent waiting for a turn freezes the
 * surface exactly as much as time spent waiting for the server — so a
 * ceiling that started at dispatch would no longer bound what it exists to
 * bound. The value itself is unchanged, and stays unchanged: the standing
 * rule from #1801 is that the timeout is not wrong, the read is slow.
 */
export const POLL_REQUEST_TIMEOUT_MS = 15_000;
