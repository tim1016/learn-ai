import { within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { reads } from '../testing/page-actions';
import type { ChainContract, ChainExpiry, UnderlyingPrice } from './past-chain.types';
import { chainExpiry, contract, HALF_DAY_CLOSE_MS, HALF_DAY_OPEN_MS, side, underlyingPrice } from './testing/fixtures';
import { chainTable, fakeChainService, openStandardChain, renderChainTab } from './testing/render-chain';

const call = (thousandths: number, changes: Partial<ChainContract> = {}): ChainContract => ({ ...contract({ right: 'call', thousandths, state: 'did_not_trade' }), ...changes });
const put = (thousandths: number, changes: Partial<ChainContract> = {}): ChainContract => ({ ...contract({ right: 'put', thousandths, state: 'did_not_trade' }), ...changes });
/** A contract that is not standard, at the same strike as a standard one: another root, so another symbol. */
const nonStandardCall = (thousandths: number): ChainContract => call(thousandths, { standard: false, steps_from_the_money: null, symbol: 'SPY1261016C00602000' });

async function open(chain: ChainExpiry) {
  await renderChainTab(fakeChainService(vi, { expiry: vi.fn(async () => chain) }));
  await openStandardChain();
}

/** Each row of the table: its strike cell, then its call cell and its put cell. */
const rowsRead = () =>
  within(chainTable())
    .getAllByRole('row')
    .slice(1)
    .map((row) => [reads(row.querySelector('th')), reads(row.querySelectorAll('td')[0] ?? null), reads(row.querySelectorAll('td')[1] ?? null)]);

describe('two contracts at one strike', () => {
  it('are two rows, with one money mark, and the put that sits in the first is not said to be missing from the second', async () => {
    const trouble = vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    const errors = vi.spyOn(console, 'error').mockImplementation(() => undefined);

    await open(chainExpiry({ calls: side('call', [call(602_000), nonStandardCall(602_000)], 602_000), puts: side('put', [put(602_000)], 602_000) }));

    const rows = rowsRead();
    expect(rows).toHaveLength(2);
    expect(rows[0]?.[0]).toBe('$602.00 At the money');
    expect(rows[1]?.[0]).toBe('$602.00');
    expect(rows[0]?.[2]).toContain("SPY Put 602 Oct 16 '26");
    expect(rows[1]?.[1]).toContain("SPY1 Call 602 Oct 16 '26");
    expect(rows[1]?.[2]).toBe('No other contract at this strike');
    expect(chainTable().textContent).not.toContain('Not listed at this strike');
    expect(within(chainTable()).getAllByText('At the money')).toHaveLength(1);
    // Two rows with one key would be reported by the framework as duplicated keys.
    expect([...trouble.mock.calls, ...errors.mock.calls].map((each) => String(each[0]))).toEqual([]);
    trouble.mockRestore();
    errors.mockRestore();
  });
});

describe('a strike one side shows and the other does not', () => {
  const windowed = () =>
    chainExpiry({
      // Each side lists far more than it shows, as the server's windows do.
      calls: side('call', [call(600_000), call(601_000), call(603_000), call(604_000)], 602_000, { listed: 120, standard_listed: 118 }),
      puts: side('put', [put(601_000), put(602_000), put(603_000)], 602_000, { listed: 118, standard_listed: 116 }),
    });

  it('is not listed only where the strikes the side shows prove it, and otherwise only that the window holds nothing', async () => {
    await open(windowed());

    expect(rowsRead().map(([strike, calls, puts]) => [strike, calls?.includes('SPY') ? 'a call' : calls, puts?.includes('SPY') ? 'a put' : puts])).toEqual([
      ['$600.00', 'a call', 'Nothing in this window'],
      ['$601.00', 'a call', 'a put'],
      ['$602.00 At the money for puts', 'Not listed at this strike', 'a put'],
      ['$603.00', 'a call', 'a put'],
      ['$604.00', 'a call', 'Nothing in this window'],
    ]);
  });

  it('is not listed anywhere when the side shows every contract it lists', async () => {
    await open(chainExpiry({ calls: side('call', [call(601_000)]), puts: side('put', [put(600_000), put(602_000)]) }));

    expect(rowsRead().map(([, calls, puts]) => [calls?.includes('SPY') ? 'a call' : calls, puts?.includes('SPY') ? 'a put' : puts])).toEqual([
      ['Not listed at this strike', 'a put'],
      ['a call', 'Not listed at this strike'],
      ['Not listed at this strike', 'a put'],
    ]);
  });
});

describe('a state this page does not know', () => {
  it('is said in words for a contract, with the code and no price, and the rest of the table is drawn', async () => {
    const odd = { ...contract({ right: 'call', thousandths: 602_000, state: 'traded', price: '2.10' }), price_state: 'halted' as ChainContract['price_state'] };

    await open(chainExpiry({ calls: side('call', [odd]), puts: side('put', [put(602_000)]) }));

    const [, calls, puts] = rowsRead()[0] ?? [];
    expect(calls).toContain('This page does not know this state of its price: Halted');
    expect(calls).not.toContain('2.10');
    expect(puts).toContain('Did not trade by then');
  });

  it('is said in words for the underlying, with the code and no price, when the chain is drawn', async () => {
    const odd = underlyingPrice({ state: 'stale' as UnderlyingPrice['state'], price: '602.40' });

    await open(chainExpiry({ underlying_price: odd }));

    const strip = reads(document.querySelector('app-options-backtest-underlying-strip'));
    expect(strip).toBe('SPY This page does not know this state of its price: Stale');
    expect(strip).not.toContain('602.40');
  });
});

describe('the chain card', () => {
  it('names the trading date beside the time it is priced at, both in New York terms', async () => {
    await open(chainExpiry({ at_ms: HALF_DAY_CLOSE_MS, session_open_ms: HALF_DAY_OPEN_MS, session_close_ms: HALF_DAY_CLOSE_MS }));

    expect(reads(document.querySelector('.head p:nth-of-type(2)'))).toBe('Priced as of 13:00 ET on 2026-11-27');
    const stamps = [...document.querySelectorAll('.head app-timestamp-display')].map((stamp) => [stamp.getAttribute('data-timestamp-mode'), reads(stamp)]);
    expect(stamps).toContainEqual(['date-et', '2026-11-27']);
    expect(stamps).toContainEqual(['et', '13:00 ET']);
  });

  it('counts listed and standard apart, and calls and puts apart', async () => {
    await open(chainExpiry({ calls: side('call', [call(602_000)], 602_000, { listed: 120, standard_listed: 118 }), puts: side('put', [put(602_000)], 602_000, { listed: 117, standard_listed: 115 }) }));

    expect(document.body.textContent).toContain('Calls: 120 listed, 118 standard. Puts: 117 listed, 115 standard.');
  });

  it('scrolls inside a region that the keyboard reaches and a reader can name', async () => {
    await open(chainExpiry());

    const region = within(document.body).getByRole('region', { name: 'Scrollable chain table' });
    expect(region.getAttribute('tabindex')).toBe('0');
    expect(region.contains(chainTable())).toBe(true);
  });
});
