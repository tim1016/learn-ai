/**
 * OCC option-symbol format utilities.
 *
 * The OCC ticker format is a single string that fully identifies an
 * option contract. Every options-related caller in this project must
 * round-trip through this module rather than parsing inline — see
 * `docs/architecture/options-routes-research.md` § R5.
 *
 * Format:
 *   O:{UNDERLYING}{YYMMDD}{C|P}{STRIKE_x1000_zero_padded_to_8}
 *
 * Example:
 *   O:SPY260220C00689000
 *     ↳ underlying: SPY
 *     ↳ expiration: 2026-02-20
 *     ↳ type:       call
 *     ↳ strike:     689.000
 */

export interface OccTickerParts {
  underlying: string;
  expirationDate: string;        // ISO YYYY-MM-DD
  contractType: 'call' | 'put';
  strike: number;                // dollar value (e.g. 689 for $689.00)
}

/**
 * Display-formatted parsing result. Intended for the contract-detail
 * header in the strategy-builder drill-down drawer (and any other
 * read-only display surface).
 *
 * If you need the raw fields for *computation* (e.g. constructing a
 * ticker for a different strike), use {@link parseOcc} instead — that
 * returns ISO dates and numeric strikes.
 */
export interface OccDisplayParts {
  underlying: string;
  expDate: string;        // e.g. "Feb 23, 2026"
  expDateShort: string;   // e.g. "02/23/26"
  type: 'Call' | 'Put';
  strike: string;         // e.g. "$690.00"
}

const OCC_REGEX = /^O:([A-Z]+)(\d{6})([CP])(\d{8})$/;
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/**
 * Parse a raw OCC ticker into structured fields. Returns null if the
 * input does not match the OCC regex.
 */
export function parseOcc(ticker: string): OccTickerParts | null {
  const match = ticker.match(OCC_REGEX);
  if (!match) return null;

  const [, underlying, dateStr, typeChar, strikeStr] = match;

  const year = 2000 + parseInt(dateStr.slice(0, 2), 10);
  const month = parseInt(dateStr.slice(2, 4), 10);
  const day = parseInt(dateStr.slice(4, 6), 10);

  const expirationDate = `${year}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
  const contractType = typeChar === 'C' ? 'call' : 'put';
  const strike = parseInt(strikeStr, 10) / 1000;

  return { underlying, expirationDate, contractType, strike };
}

/**
 * Parse a raw OCC ticker into display-ready fields ("Feb 23, 2026",
 * "$690.00", "Call"). Returns null if the input does not match.
 *
 * Display fields are derived from {@link parseOcc} — round-trip parity
 * is enforced by the spec next to this file.
 */
export function parseOccForDisplay(ticker: string): OccDisplayParts | null {
  const parsed = parseOcc(ticker);
  if (!parsed) return null;

  const [yearStr, monthStr, dayStr] = parsed.expirationDate.split('-');
  const year = parseInt(yearStr, 10);
  const month = parseInt(monthStr, 10);
  const day = parseInt(dayStr, 10);

  return {
    underlying: parsed.underlying,
    expDate: `${MONTHS[month - 1]} ${day}, ${year}`,
    expDateShort: `${String(month).padStart(2, '0')}/${String(day).padStart(2, '0')}/${String(year).slice(2)}`,
    type: parsed.contractType === 'call' ? 'Call' : 'Put',
    strike: `$${parsed.strike.toFixed(2)}`,
  };
}

/**
 * Construct a raw OCC ticker from structured fields. The inverse of
 * {@link parseOcc} — round-trip parity is enforced by the spec.
 *
 * Throws if the inputs are obviously invalid (negative strike, bad
 * date, empty underlying).
 */
export function formatOcc(parts: OccTickerParts): string {
  const { underlying, expirationDate, contractType, strike } = parts;

  if (!underlying || !/^[A-Z]+$/.test(underlying)) {
    throw new Error(`OCC: invalid underlying "${underlying}"`);
  }
  if (strike <= 0) {
    throw new Error(`OCC: strike must be positive, got ${strike}`);
  }

  const dateMatch = expirationDate.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!dateMatch) {
    throw new Error(`OCC: expirationDate must be ISO YYYY-MM-DD, got "${expirationDate}"`);
  }
  const [, yyyy, mm, dd] = dateMatch;

  const yy = yyyy.slice(2);
  const cp = contractType === 'call' ? 'C' : 'P';
  // Strike is encoded as integer thousandths, zero-padded to 8 digits.
  // e.g. 689.0 → "00689000", 0.5 → "00000500"
  const strikeStr = String(Math.round(strike * 1000)).padStart(8, '0');

  return `O:${underlying}${yy}${mm}${dd}${cp}${strikeStr}`;
}
