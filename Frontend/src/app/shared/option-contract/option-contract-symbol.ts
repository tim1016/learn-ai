import { parseIsoDate } from '../date/iso-date';
import type { OptionContract } from './option-contract.component';
import { optionExpiryAnchorMs } from './option-expiry-date';

/**
 * Decode an OCC identifier only for display. The broker's original code stays
 * intact; order validation belongs to PythonDataService/app/options/contract.py.
 * The strike's decimal point is inserted as text, without floating point math.
 */
export function optionContractFromSymbol(symbol: string, underlying?: string | null): OptionContract | null {
  const match = /^(?:O:)?([A-Z]{1,6}\d?)\s*(\d{6})([CP])(\d{8})$/.exec(symbol.trim().toUpperCase());
  if (!match) return null;
  const [, root, date, right, digits] = match;
  const knownUnderlying = underlying?.trim().toUpperCase() || null;
  const expiry = parseIsoDate(`20${date.slice(0, 2)}-${date.slice(2, 4)}-${date.slice(4, 6)}`);
  if (expiry === null || /^0+$/.test(digits)) return null;
  const whole = digits.slice(0, 5).replace(/^0+(?=\d)/, '');
  const fraction = digits.slice(5).replace(/0+$/, '');
  return {
    occ_symbol: symbol,
    underlying: knownUnderlying ?? root,
    underlyingIsRoot: knownUnderlying === null,
    expiry_ms: optionExpiryAnchorMs(expiry),
    right: right === 'C' ? 'call' : 'put',
    strike: fraction ? `${whole}.${fraction}` : whole,
  };
}
