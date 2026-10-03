/**
 * Walks a ``YYYY-MM-DD`` ISO date string back to the most recent
 * Mon-Fri, returning a fresh ``YYYY-MM-DD`` string. Operates entirely
 * in UTC so the result is independent of the caller's browser
 * timezone: ``new Date("YYYY-MM-DD")`` parses as UTC midnight, the prior
 * local evening west of UTC, so a local-time walk can re-serialize a
 * weekend. No calendar arithmetic here crosses the local/UTC boundary.
 *
 * Holidays are NOT handled here — a caller that needs a real session
 * asks the trading calendar (``shared/date-picker``).
 *
 * Pass a non-zero ``daysOffset`` to shift first, then walk. This
 * expresses "30 calendar days before ``last``, snapped to the most
 * recent weekday" in a single call without an intermediate ``Date``
 * round-trip — the pattern ``instrument-card.pickTicker`` needs.
 */
export function toMostRecentTradingDayIso(iso: string, daysOffset = 0): string {
  const [year, month, day] = iso.split("-").map(Number);
  const date = new Date(Date.UTC(year, month - 1, day));
  if (daysOffset !== 0) {
    date.setUTCDate(date.getUTCDate() + daysOffset);
  }
  while (date.getUTCDay() === 0 || date.getUTCDay() === 6) {
    date.setUTCDate(date.getUTCDate() - 1);
  }
  return date.toISOString().slice(0, 10);
}

/**
 * The mirror of {@link toMostRecentTradingDayIso}: walks a ``YYYY-MM-DD``
 * date *forward* to the next Mon-Fri, in UTC, for the same reason that one
 * walks backward in UTC.
 *
 * Use this wherever the date being snapped is a **floor** — the oldest day a
 * provider will serve, the first day of a held span — because walking such a
 * date backward off a weekend steps it outside the very bound it represents.
 * Holidays are still not handled here, by the same deliberate choice.
 */
export function toNextTradingDayIso(iso: string): string {
  const [year, month, day] = iso.split("-").map(Number);
  const date = new Date(Date.UTC(year, month - 1, day));
  while (date.getUTCDay() === 0 || date.getUTCDay() === 6) {
    date.setUTCDate(date.getUTCDate() + 1);
  }
  return date.toISOString().slice(0, 10);
}
