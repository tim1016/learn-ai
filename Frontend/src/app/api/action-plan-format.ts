/**
 * Pure display formatters for the operator-declared action plan.
 *
 * Shared between the cockpit card (Slice 1A+1B+1C), the picker (option
 * editing in later slices), the parity-preview warning UI (Slice 1D),
 * and the redeploy form (Slice 1E). Keeping them in one module avoids
 * copy-paste between component files and gives the formatters a stable,
 * unit-testable home outside any Angular component.
 *
 * ADR 0012 / repo timestamp policy: ``absolute.expiration_ms`` is
 * ``int64`` ms UTC at rest and on the wire; the only place it should
 * become an ``America/New_York`` wall-clock string is here, at the
 * rendering boundary.
 */

import type { OptionEntryLeg } from './action-plan.types';

/** ``"Long call · ATM · min_dte 14d"`` etc. Display only — the stored
 * leg remains authoritative. */
export function optionSummary(leg: OptionEntryLeg): string {
  const direction = leg.position === 'long' ? 'Long' : 'Short';
  return `${direction} ${leg.right} · ${formatStrike(leg)} · ${formatExpiry(leg)}`;
}

export function formatStrike(leg: OptionEntryLeg): string {
  const s = leg.strike;
  switch (s.selector) {
    case 'atm':
      return 'ATM';
    case 'atm_offset':
      return s.offset >= 0 ? `ATM+${s.offset}` : `ATM${s.offset}`;
    case 'absolute':
      return `$${s.strike}`;
  }
}

export function formatExpiry(leg: OptionEntryLeg): string {
  const e = leg.expiry;
  switch (e.selector) {
    case 'min_dte':
      return `min_dte ${e.days}d`;
    case 'nearest_weekly':
      return 'nearest weekly';
    case 'absolute':
      return formatNyDate(e.expiration_ms);
  }
}

/** ``int64`` ms UTC → ``YYYY-MM-DD`` in America/New_York. */
export function formatNyDate(ms: number): string {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'America/New_York',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(new Date(ms));
  const yyyy = parts.find((p) => p.type === 'year')?.value ?? '';
  const mm = parts.find((p) => p.type === 'month')?.value ?? '';
  const dd = parts.find((p) => p.type === 'day')?.value ?? '';
  return `${yyyy}-${mm}-${dd}`;
}
