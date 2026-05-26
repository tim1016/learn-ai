/**
 * Walks a date back to the most recent Mon-Fri, in the same calendar
 * day semantics as the caller's local timezone.
 *
 * Use this for call sites that already operate on local-time ``Date``
 * instances — typically ``new Date()`` + ``setHours(0, 0, 0, 0)`` to
 * pin local midnight, then this helper for the weekday walk. The
 * caller is responsible for serializing the result without crossing
 * a local-vs-UTC day boundary.
 *
 * For ``YYYY-MM-DD`` ISO inputs (server payloads, picker model
 * fields), use :func:`toMostRecentTradingDayIso` instead.
 * ``new Date("YYYY-MM-DD")`` parses as **UTC** midnight per
 * ECMA-262 §21.4.3.2, which in west-of-UTC timezones is the prior
 * local evening; a local-time walk plus UTC-based ``toISOString``
 * round-trip can then re-introduce a weekend endpoint, which is
 * exactly the regression PR #346 was meant to prevent and review
 * caught a follow-up of in ``instrument-card.pickTicker``.
 *
 * Holidays are NOT handled here — server still returns 422 for those,
 * by design, so they surface visibly instead of being silently bumped.
 *
 * Pure, no side effects. Returns a new ``Date`` and never mutates ``d``.
 */
export function toMostRecentWeekday(d: Date): Date {
  const out = new Date(d);
  while (out.getDay() === 0 || out.getDay() === 6) {
    out.setDate(out.getDate() - 1);
  }
  return out;
}

/**
 * Walks a ``YYYY-MM-DD`` ISO date string back to the most recent
 * Mon-Fri, returning a fresh ``YYYY-MM-DD`` string. Operates entirely
 * in UTC so the result is independent of the caller's browser
 * timezone — the bug PR #346 review caught is structurally impossible
 * here because no calendar arithmetic crosses the local/UTC boundary.
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
