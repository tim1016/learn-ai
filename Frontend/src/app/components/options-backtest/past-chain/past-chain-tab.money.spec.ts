import { screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { reads } from '../testing/page-actions';
import { chainExpiry, contract, side, underlyingPrice } from './testing/fixtures';
import { chainTable, fakeChainService, openStandardChain, renderChainTab } from './testing/render-chain';

/** 2^53 + 1 is the first whole number a float cannot hold; a cent after it is a figure only a string keeps. */
const BEYOND_A_FLOAT = '9007199254740993.01';

async function openWith(chain: ReturnType<typeof chainExpiry>) {
  await renderChainTab(fakeChainService(vi, { expiry: vi.fn(async () => chain) }));
  await openStandardChain();
}

const call = (thousandths: number, price: string, strike?: string) => ({ ...contract({ right: 'call', thousandths, state: 'traded', price }), ...(strike === undefined ? {} : { strike }) });
const put = (thousandths: number, price: string) => contract({ right: 'put', thousandths, state: 'traded', price });

describe('the money on the Past chain tab is the string the server sent', () => {
  it.each([
    ['keeps a price a float cannot hold, to the cent', BEYOND_A_FLOAT, '$9,007,199,254,740,993.01 in the minute ending 15:59 ET'],
    ['pads a whole-cent price to cents and groups thousands without parsing it', '1234.5', '$1,234.50 in the minute ending'],
  ])('%s', async (_name, price, shown) => {
    await openWith(chainExpiry({ calls: side('call', [call(602_000, price)]), puts: side('put', []) }));

    expect(reads(chainTable().querySelector('app-options-backtest-chain-contract'))).toContain(shown);
  });

  it('keeps the underlying price a float cannot hold, to the cent', async () => {
    await openWith(chainExpiry({ underlying_price: underlyingPrice({ price: BEYOND_A_FLOAT }) }));

    expect(reads(screen.getByRole('group', { name: 'The underlying at that time' }))).toBe('SPY $9,007,199,254,740,993.01 in the minute ending 16:00 ET');
  });

  it('shows a price of four decimals as the server wrote it, and not as a figure it re-rounded', async () => {
    await openWith(chainExpiry({ calls: side('call', [call(602_000, '0.4250')]), puts: side('put', [put(602_000, '1.2')]) }));

    const cells = [...chainTable().querySelectorAll('app-options-backtest-chain-contract')].map((cell) => reads(cell));

    expect(cells[0]).toContain('0.4250 in the minute ending 15:59 ET');
    expect(cells[0]).not.toContain('$0.43');
    expect(cells[0]).not.toContain('0.43');
    expect(cells[1]).toContain('$1.20 in the minute ending 15:59 ET');
  });

  it('shows a strike as the server wrote it: whole cents with a dollar sign, anything finer untouched', async () => {
    await openWith(chainExpiry({ calls: side('call', [call(602_500, '1.00'), call(603_125, '1.00')]), puts: side('put', []) }));

    const strikes = [...chainTable().querySelectorAll('th[scope="row"] .price')].map((strike) => reads(strike));

    expect(strikes).toEqual(['$602.50', '603.125']);
  });

  it('draws no dollar amount anywhere on the table that the server did not send', async () => {
    await openWith(chainExpiry({ calls: side('call', [call(602_000, BEYOND_A_FLOAT)]), puts: side('put', [put(602_000, '0.4250')]) }));

    const amounts = (chainTable().textContent ?? '').match(/\$[\d,]+(?:\.\d+)?/g) ?? [];

    // In the order of the row: the call price, then the strike. The put price (0.4250) is not an authored amount and passes through bare.
    expect(amounts).toEqual(['$9,007,199,254,740,993.01', '$602.00']);
  });
});
