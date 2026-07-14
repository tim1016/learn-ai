/**
 * Shared formatters for broker pages.
 *
 * All numbers cross the wire as snake_case `number | null` per the
 * Pydantic models — see `app.broker.ibkr.models`. UI-side, render with
 * Intl so locale grouping is consistent across pages.
 */
import { formatTimestampDisplay } from '../../shared/timestamp';

const CURRENCY = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const SIGNED_CURRENCY = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
  signDisplay: 'exceptZero',
});

const INTEGER = new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 });

const SIGNED_INTEGER = new Intl.NumberFormat('en-US', {
  maximumFractionDigits: 0,
  signDisplay: 'exceptZero',
});

const PERCENT = new Intl.NumberFormat('en-US', {
  style: 'percent',
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

export function fmtCurrency(value: number | null | undefined): string {
  return value == null ? '—' : CURRENCY.format(value);
}

export function fmtSignedCurrency(value: number | null | undefined): string {
  return value == null ? '—' : SIGNED_CURRENCY.format(value);
}

export function fmtInteger(value: number | null | undefined): string {
  return value == null ? '—' : INTEGER.format(value);
}

export function fmtSignedInteger(value: number | null | undefined): string {
  return value == null ? '—' : SIGNED_INTEGER.format(value);
}

export function fmtPercent(
  value: number | null | undefined,
  fractionDigits = 2,
): string {
  if (value == null) return '—';
  const formatter = new Intl.NumberFormat('en-US', {
    style: 'percent',
    minimumFractionDigits: fractionDigits,
    maximumFractionDigits: fractionDigits,
  });
  return formatter.format(value);
}

export function fmtPercentDefault(value: number | null | undefined): string {
  return value == null ? '—' : PERCENT.format(value);
}

export function fmtSignedNumber(
  value: number | null | undefined,
  fractionDigits = 2,
): string {
  if (value == null) return '—';
  const formatter = new Intl.NumberFormat('en-US', {
    minimumFractionDigits: fractionDigits,
    maximumFractionDigits: fractionDigits,
    signDisplay: 'exceptZero',
  });
  return formatter.format(value);
}

export function fmtNumber(
  value: number | null | undefined,
  fractionDigits = 2,
): string {
  if (value == null) return '—';
  const formatter = new Intl.NumberFormat('en-US', {
    minimumFractionDigits: fractionDigits,
    maximumFractionDigits: fractionDigits,
  });
  return formatter.format(value);
}

/** Format an `int64 ms UTC` timestamp in the viewer's browser-local timezone. */
export function fmtTimestampLocal(ms: number | null | undefined): string {
  return formatTimestampDisplay(ms, { mode: 'local' });
}

/** Format an `int64 ms UTC` timestamp as a New York wall-clock string for display. */
export function fmtTimestampNy(ms: number | null | undefined): string {
  return formatTimestampDisplay(ms, { mode: 'et' });
}

/** Same as ``fmtTimestampNy`` but date-only in New York time. */
export function fmtDateNy(ms: number | null | undefined): string {
  return formatTimestampDisplay(ms, { mode: 'date-et' });
}

/** Format broker expiry markers that encode IBKR ``YYYYMMDD`` as midnight UTC. */
export function fmtBrokerExpiryDate(ms: number | null | undefined): string {
  return formatTimestampDisplay(ms, { mode: 'date-utc' });
}

/** Format a live freshness countdown without deriving any domain state. */
export function fmtDurationRemaining(remainingMs: number): string {
  if (remainingMs <= 0) return 'Expired';
  const totalSeconds = Math.ceil(remainingMs / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return minutes > 0 ? `${minutes}m ${seconds}s` : `${seconds}s`;
}

/** Format elapsed observation freshness; this is display-only and owns no state. */
export function fmtElapsedSince(observedAtMs: number, nowMs: number = Date.now()): string {
  const totalSeconds = Math.max(0, Math.floor((nowMs - observedAtMs) / 1000));
  if (totalSeconds < 60) return `${totalSeconds}s ago`;
  const totalMinutes = Math.floor(totalSeconds / 60);
  if (totalMinutes < 60) return `${totalMinutes}m ago`;
  return `${Math.floor(totalMinutes / 60)}h ago`;
}

/**
 * Compare two scalars and return their divergence in basis points.
 * Returns ``null`` when either side is missing or the reference is zero
 * (we cannot express "X bps off zero" — caller decides how to display).
 *
 * Appropriate for **unbounded** quantities like dollar values where the
 * reference rarely lands near zero (cash, NLV, P&L). For **bounded**
 * quantities like delta where the reference can legitimately be ~0 in
 * the wings of the chain, prefer ``absDiff`` — dividing by a near-zero
 * reference amplifies a tiny absolute disagreement into millions of
 * bps and produces meaningless red cells.
 */
export function diffBps(
  measured: number | null | undefined,
  reference: number | null | undefined,
): number | null {
  if (measured == null || reference == null) return null;
  if (reference === 0) return null;
  return ((measured - reference) / Math.abs(reference)) * 10_000;
}

/**
 * Absolute disagreement between two scalars in their native units.
 * Returns ``null`` when either side is missing.
 *
 * Stable alternative to ``diffBps`` for bounded quantities like delta
 * (range [-1, 1]) — ``|measured - reference|`` doesn't blow up when
 * the reference is near zero. Pair with ``deltaAbsBand`` for the
 * reconciliation tolerance bands.
 */
export function absDiff(
  measured: number | null | undefined,
  reference: number | null | undefined,
): number | null {
  if (measured == null || reference == null) return null;
  return Math.abs(measured - reference);
}

export type ToleranceBand = 'green' | 'yellow' | 'red';

/**
 * Map a basis-points divergence to the reconciliation tolerance band
 * documented in ``ibkr-frontend-implementation-plan.md`` §9.2.
 */
export function toleranceBand(diffBpsValue: number | null): ToleranceBand | null {
  if (diffBpsValue === null) return null;
  const abs = Math.abs(diffBpsValue);
  if (abs <= 50) return 'green';
  if (abs <= 200) return 'yellow';
  return 'red';
}

/**
 * Map an absolute delta disagreement to a tolerance band. Thresholds
 * picked so an at-the-money disagreement of 50 bps (delta ~0.5 → 0.005
 * absolute) is yellow and 200 bps (0.02 absolute) is red — same
 * semantics as ``toleranceBand`` at the center of the curve, but
 * stable in the wings where delta is small.
 */
export function deltaAbsBand(diff: number | null): ToleranceBand | null {
  if (diff === null) return null;
  const abs = Math.abs(diff);
  if (abs <= 0.005) return 'green';
  if (abs <= 0.02) return 'yellow';
  return 'red';
}
