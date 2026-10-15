import { describe, expect, it } from 'vitest';

import { optionContractFromSymbol } from './option-contract-symbol';
import { optionExpiryDate } from './option-expiry-date';

describe('optionContractFromSymbol', () => {
  it.each([
    ['SPY261009C00587500', 'SPY', 'call', '587.5'],
    ['O:SPY261009P00585000', 'SPY', 'put', '585'],
    ['AAPL  270115C00000125', 'AAPL', 'call', '0.125'],
    ['spy261009c00585000', 'SPY', 'call', '585'],
    ['AAPL1261009P00100000', 'AAPL1', 'put', '100'],
  ])('reads %s for display, keeping its original broker identifier', (symbol, underlying, right, strike) => {
    const contract = optionContractFromSymbol(symbol);
    expect(contract).toMatchObject({ occ_symbol: symbol, underlying, right, strike });
    expect(contract && optionExpiryDate(contract.expiry_ms)).toBe(symbol.includes('270115') ? '2027-01-15' : '2026-10-09');
  });

  it.each(['SPY', 'BTC/USD', 'SPY261332C00585000', 'SPY260229C00585000', 'SPY261009X00585000', 'SPY261009C00000000', 'SPY261009C0058500', 'SPY261009C00585000junk'])('leaves an ordinary or malformed symbol alone: %s', (symbol) => {
    expect(optionContractFromSymbol(symbol)).toBeNull();
  });
});
