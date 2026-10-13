/**
 * OCC option-symbol format utilities.
 *
 * The OCC ticker format is a single string that fully identifies an
 * option contract. Every options-related caller in this project must
 * round-trip through this module rather than parsing inline.
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
 * Days-in-month for a given (year, 1-indexed month). Matches the Gregorian
 * calendar used everywhere we render expirations. Used to reject
 * structurally-valid but calendar-invalid OCC dates (e.g. month 13,
 * Feb 30) before they reach a downstream consumer that would convert
 * them via `new Date(...)` and silently roll over.
 */
function daysInMonth(year: number, month: number): number {
  if (month < 1 || month > 12) return 0;
  if (month === 2) {
    const leap = (year % 4 === 0 && year % 100 !== 0) || year % 400 === 0;
    return leap ? 29 : 28;
  }
  return [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1];
}

function isValidGregorianDate(year: number, month: number, day: number): boolean {
  if (month < 1 || month > 12) return false;
  if (day < 1) return false;
  return day <= daysInMonth(year, month);
}

/**
 * Parse a raw OCC ticker into structured fields. Returns null if the
 * input does not match the OCC regex or encodes a calendar-invalid
 * date (month 13, Feb 30, etc.).
 */
export function parseOcc(ticker: string): OccTickerParts | null {
  const match = ticker.match(OCC_REGEX);
  if (!match) return null;

  const [, underlying, dateStr, typeChar, strikeStr] = match;

  const year = 2000 + parseInt(dateStr.slice(0, 2), 10);
  const month = parseInt(dateStr.slice(2, 4), 10);
  const day = parseInt(dateStr.slice(4, 6), 10);

  if (!isValidGregorianDate(year, month, day)) return null;

  const expirationDate = `${year}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
  const contractType = typeChar === 'C' ? 'call' : 'put';
  const strike = parseInt(strikeStr, 10) / 1000;

  return { underlying, expirationDate, contractType, strike };
}

/**
 * Parse a raw OCC ticker into display-ready fields ("Feb 23, 2026",
 * "$690.00", "Call"). Returns null if the input does not match.
 *
 * Display fields are derived from {@link parseOcc}, so the two always agree.
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
