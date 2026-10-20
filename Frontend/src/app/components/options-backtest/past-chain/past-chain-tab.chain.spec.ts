import { screen, waitFor, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { paragraph, reads } from '../testing/page-actions';
import { PastChainRefusedError } from './past-chain.service';
import type { ChainExpiry } from './past-chain.types';
import { chainDay, chainExpiry, contract, EXPIRY_MS, MINUTE_1559_MS, side, summary, tag, TRADING_DATE, TRADING_DATE_MS, underlyingPrice } from './testing/fixtures';
import { assertNewYorkTimes, chainTable, expiriesTable, fakeChainService, openStandardChain, pickDate, renderChainTab, showChain } from './testing/render-chain';

/** The row of the chain table whose strike reads `strike` (the dollars as shown). */
function rowOf(strike: string): HTMLElement {
  const row = within(chainTable())
    .getAllByRole('row')
    .find((candidate) => reads(candidate.querySelector('th[scope="row"]')).startsWith(strike));
  if (row === undefined) throw new Error(`No row of the chain reads ${strike}.`);
  return row;
}

const cellsOf = (row: HTMLElement) => [...row.querySelectorAll('td')].map((cell) => reads(cell));

describe('the chain of one expiry', () => {
  it('is read once when an expiry is opened, with the expiry as the server gave it, and is drawn with calls on the left, the money strike marked, and every contract in words or by its priced trade', async () => {
    const service = fakeChainService(vi);
    await renderChainTab(service);

    await openStandardChain();

    expect.soft(service.expiry, 'is read when an expiry is opened').toHaveBeenCalledTimes(1);
    expect.soft(service.expiry.mock.calls[0]?.[0], 'is read with the expiry as the server gave it and neither an instant nor a number of strikes the owner did not choose').toEqual({
      underlying: 'SPY',
      tradingDateMs: TRADING_DATE_MS,
      expiryMs: EXPIRY_MS['2026-10-16'],
      atMs: null,
      strikes: null,
    });
    const headers = within(chainTable()).getAllByRole('columnheader').map((header) => reads(header));
    const strikes = within(chainTable())
      .getAllByRole('rowheader')
      .map((header) => reads(header).split(' ')[0]);
    expect.soft(headers, 'puts calls on the left, the strikes down the middle and puts on the right').toEqual(['Calls', 'Strike', 'Puts']);
    expect.soft(strikes, 'puts the strikes in strike order').toEqual(['$601.00', '$602.00', '$603.00']);
    expect.soft(rowOf('$601.00').querySelectorAll('td')[0]?.querySelector('[aria-label]')?.getAttribute('aria-label'), 'names the call contract of the first strike').toBe("SPY Oct 16 '26 601 Call");
    expect.soft(rowOf('$601.00').querySelectorAll('td')[1]?.querySelector('[aria-label]')?.getAttribute('aria-label'), 'names the put contract of the first strike').toBe("SPY Oct 16 '26 601 Put");
    expect.soft(reads(rowOf('$602.00').querySelector('th')), 'marks the money strike as the server served it').toContain('At the money');
    expect.soft(reads(rowOf('$601.00').querySelector('th')), 'does not mark the strike below the money strike').not.toContain('At the money');
    expect.soft(reads(rowOf('$603.00').querySelector('th')), 'does not mark the strike above the money strike').not.toContain('At the money');
    // 19:59 UTC is 15:59 in New York in October; 19:58 UTC is 15:58.
    expect.soft(cellsOf(rowOf('$602.00'))[0], 'says, for a contract that traded, the price and the time of that trade in New York time: the 602 call').toMatch(/\$2\.10 in the minute ending 15:59 ET/);
    expect.soft(cellsOf(rowOf('$601.00'))[0], 'says, for a contract that traded, the price and the time of that trade in New York time: the 601 call').toMatch(/\$3\.25 in the minute ending 15:58 ET/);
    expect.soft(cellsOf(rowOf('$603.00'))[1], 'says, for a contract that traded, the price and the time of that trade in New York time: the 603 put').toMatch(/\$1\.05 in the minute ending 15:45 ET/);
    const prices = [...chainTable().querySelectorAll('app-options-backtest-chain-contract .price')];
    expect.soft(prices.length, 'shows three prices in the table').toBe(3);
    for (const price of prices) {
      expect.soft(reads(price.closest('app-options-backtest-chain-contract')), 'never shows a price without a time: every price in the table has the time of its trade beside it').toMatch(/\$\d+\.\d{2} in the minute ending \d{2}:\d{2} ET/);
    }
    expect.soft(cellsOf(rowOf('$603.00'))[0], 'says in words that a contract did not trade').toContain('Did not trade by then');
    expect.soft(cellsOf(rowOf('$601.00'))[1], 'says in words that a contract has no prices stored').toContain('Listed; prices not stored');
    expect.soft(cellsOf(rowOf('$602.00'))[1], 'says in words that a contract cannot be read').toContain('Prices cannot be read');
    expect.soft(cellsOf(rowOf('$602.00'))[1], 'says in words why a contract cannot be read').toContain('the stored file is damaged (OptionBarsFileError)');
    for (const cell of chainTable().querySelectorAll('td')) expect.soft(reads(cell), 'never shows a blank cell').not.toBe('');
    expect.soft(chainTable().textContent, 'never shows a zero for a missing price').not.toMatch(/\$0\.00/);
  });

  it('is not read before the expiry is picked, and the page asks for a pick', async () => {
    const service = fakeChainService(vi);
    await renderChainTab(service);

    pickDate(TRADING_DATE);
    await screen.findByRole('table', { name: /Every expiry the listing held/ });

    expect(service.expiry).not.toHaveBeenCalled();
    expect(screen.getByText('No expiry is open. Open one above to read its chain.')).toBeTruthy();
  });

  it('marks calls and puts apart when their money strikes differ', async () => {
    const service = fakeChainService(vi, {
      expiry: vi.fn(async () =>
        chainExpiry({
          calls: side('call', [contract({ right: 'call', thousandths: 602_000, state: 'did_not_trade' }), contract({ right: 'call', thousandths: 603_000, state: 'did_not_trade' })], 602_000),
          puts: side('put', [contract({ right: 'put', thousandths: 602_000, state: 'did_not_trade' }), contract({ right: 'put', thousandths: 603_000, state: 'did_not_trade' })], 603_000),
        }),
      ),
    });
    await renderChainTab(service);
    await openStandardChain();

    expect(reads(rowOf('$602.00').querySelector('th'))).toContain('At the money for calls');
    expect(reads(rowOf('$603.00').querySelector('th'))).toContain('At the money for puts');
  });

  it('shows no price at all for a trade the server sent without its time, and says so', async () => {
    const odd = { ...contract({ right: 'call', thousandths: 602_000, state: 'traded', price: '2.10' }), price_time_ms: null };
    const service = fakeChainService(vi, { expiry: vi.fn(async () => chainExpiry({ calls: side('call', [odd]), puts: side('put', []) })) });
    await renderChainTab(service);
    await openStandardChain();

    const cell = cellsOf(rowOf('$602.00'))[0];

    expect(cell).toContain('Traded, but the server sent no time for the trade, so none is shown');
    expect(cell).not.toContain('2.10');
  });

  it('labels a contract that is not standard, and does not count it among the ladder', async () => {
    const odd = contract({ right: 'call', thousandths: 602_500, state: 'did_not_trade', standard: false });
    const service = fakeChainService(vi, { expiry: vi.fn(async () => chainExpiry({ calls: side('call', [odd]), puts: side('put', []) })) });
    await renderChainTab(service);
    await openStandardChain();

    expect(cellsOf(rowOf('$602.50'))[0]).toContain('Non-standard');
  });

  it('says a strike is not listed on a side that has no contract at it', async () => {
    const service = fakeChainService(vi, {
      expiry: vi.fn(async () =>
        chainExpiry({
          calls: side('call', [contract({ right: 'call', thousandths: 601_000, state: 'did_not_trade' })]),
          puts: side('put', [contract({ right: 'put', thousandths: 602_000, state: 'did_not_trade' })]),
        }),
      ),
    });
    await renderChainTab(service);
    await openStandardChain();

    expect(cellsOf(rowOf('$601.00'))[1]).toBe('Not listed at this strike');
    expect(cellsOf(rowOf('$602.00'))[0]).toBe('Not listed at this strike');
  });

  it('shows the series and the expires-today badge of the expiry that is open, and the instant it is priced at', async () => {
    const service = fakeChainService(vi, {
      expiry: vi.fn(async () => chainExpiry({ tag: { series: 'monthly', version: 1, evidence: 'the third Friday of its month', expires_today: true, days_to_expiry: 0 } })),
    });
    await renderChainTab(service);
    await openStandardChain();

    const head = reads(paragraph(/^Priced as of/).closest('.head'));

    expect(head).toContain('Expiry 2026-10-16');
    expect(head).toContain('Monthly');
    expect(head).toContain('Expires today');
    expect(head).toContain('Priced as of 16:00 ET on 2026-10-09');
    expect(head).toContain('Strikes each side of the money: 10');
  });
});

describe('the underlying at that time', () => {
  async function openWith(price: Parameters<typeof underlyingPrice>[0]) {
    const service = fakeChainService(vi, { expiry: vi.fn(async () => chainExpiry({ underlying_price: underlyingPrice(price) })) });
    await renderChainTab(service);
    await openStandardChain();
    return reads(screen.getByRole('group', { name: 'The underlying at that time' }));
  }

  it.each([
    [{ price: '602.40', price_time_ms: MINUTE_1559_MS }, '$602.40 in the minute ending 15:59 ET'],
    [{ state: 'no_bar_yet', price: null, price_time_ms: null }, 'No bar has closed by then'],
    [{ state: 'not_stored', price: null, price_time_ms: null }, 'Its bars are not stored'],
    [{ state: 'unreadable', price: null, price_time_ms: null, problem: 'the bars of SPY cannot be read (UnderlyingDayUnreadable)' }, 'Its bars cannot be read the bars of SPY cannot be read (UnderlyingDayUnreadable)'],
    [{ state: 'priced', price: '602.40', price_time_ms: null }, 'Priced, but the server sent no time for it, so none is shown'],
    [{ state: 'priced', price: null, price_time_ms: MINUTE_1559_MS }, 'Priced, but the server sent no price for it, so none is shown'],
  ] as const)('is, for %j, said in words', async (price, words) => {
    expect(await openWith(price)).toBe(`SPY ${words}`);
  });
});

describe('an expiry that the date does not list', () => {
  it('is said plainly, the day expiries stay on screen, and no other expiry is opened', async () => {
    const service = fakeChainService(vi, { expiry: vi.fn(async () => chainExpiry({ expiry_listed: false, tag: null, calls: side('call', [], null), puts: side('put', [], null) })) });
    await renderChainTab(service);

    pickDate(TRADING_DATE);
    await screen.findByRole('table', { name: /Every expiry the listing held/ });
    showChain('2026-10-16');
    await waitFor(() => expect(paragraph(/expiry is not listed on this date/)).toBeTruthy());

    expect(reads(paragraph(/expiry is not listed on this date/))).toContain('The 2026-10-16 expiry is not listed on this date.');
    expect(expiriesTable()).toBeTruthy();
    expect(screen.queryByRole('table', { name: /Calls on the left/ })).toBeNull();
    expect(service.expiry).toHaveBeenCalledTimes(1);
  });

  it('is told apart from not knowing whether it is listed, when the listing is not stored or cannot be read', async () => {
    const service = fakeChainService(vi, {
      expiry: vi.fn(async () => chainExpiry({ expiry_listed: null, tag: null, listing_state: 'unreadable', listing_problem: 'the listing cannot be read (OptionListingFileError)', calls: side('call', [], null), puts: side('put', [], null) })),
    });
    await renderChainTab(service);
    pickDate(TRADING_DATE);
    await screen.findByRole('table', { name: /Every expiry the listing held/ });
    showChain('2026-10-16');

    await waitFor(() => expect(paragraph(/Whether the .* expiry is listed cannot be said/)).toBeTruthy());
    expect(reads(paragraph(/Whether the .* expiry is listed cannot be said/))).toBe('Whether the 2026-10-16 expiry is listed cannot be said: the listing for this date is stored but cannot be read.');
    expect(screen.getByText('the listing cannot be read (OptionListingFileError)')).toBeTruthy();
  });
});

describe('an expiry whose listing is in a state this page does not know', () => {
  it.each([
    ['with the expiry listed', { expiry_listed: true }],
    ['with the expiry not listed', { expiry_listed: false, tag: null }],
    ['with no answer on whether the expiry is listed', { expiry_listed: null, tag: null }],
  ] as const)('says the code in words, %s, and neither draws a chain nor says the listing is not stored', async (_name, changes) => {
    const odd = chainExpiry({ ...changes, listing_state: 'partial' as ChainExpiry['listing_state'] });
    await renderChainTab(fakeChainService(vi, { expiry: vi.fn(async () => odd) }));
    pickDate(TRADING_DATE);
    await screen.findByRole('table', { name: /Every expiry the listing held/ });

    showChain('2026-10-16');

    const note = await screen.findByText(/This page does not know this state of the listing/);
    expect(reads(note)).toBe('This page does not know this state of the listing, Partial, so it does not say whether the 2026-10-16 expiry is listed.');
    expect(screen.queryByRole('table', { name: /Calls on the left/ })).toBeNull();
    expect(screen.queryByText(/not stored/)).toBeNull();
  });
});

describe('the trading date and the expiry', () => {
  it('are separate controls: a new date leaves the open expiry as it was, and it is read again on the new date', async () => {
    const service = fakeChainService(vi);
    await renderChainTab(service);
    await openStandardChain();

    pickDate('2026-10-12');
    await waitFor(() => expect(service.expiry).toHaveBeenCalledTimes(2));

    // New York midnight of 2026-10-12 is 04:00 UTC (daylight time).
    expect(service.expiry.mock.calls[1]?.[0]).toMatchObject({ tradingDateMs: 1_791_777_600_000, expiryMs: EXPIRY_MS['2026-10-16'] });
    expect((screen.getByLabelText('Trading date') as HTMLInputElement).value).toBe('2026-10-12');
    expect(within(expiryRow16()).getByRole('button', { name: /Show chain/ }).getAttribute('aria-pressed')).toBe('true');
  });

  it('are separate controls: opening an expiry leaves the date as it was', async () => {
    await renderChainTab(fakeChainService(vi));
    pickDate(TRADING_DATE);
    await screen.findByRole('table', { name: /Every expiry the listing held/ });

    showChain('2026-10-30');
    await screen.findByRole('table', { name: /Calls on the left/ });

    expect((screen.getByLabelText('Trading date') as HTMLInputElement).value).toBe(TRADING_DATE);
  });

  it('keeps the expiry open when the new date does not list it, says so, and lists that date expiries without opening another', async () => {
    const day = vi
      .fn()
      .mockResolvedValueOnce(chainDay())
      .mockResolvedValue(chainDay({ expiries: [summary(EXPIRY_MS['2026-10-30'], tag('weekly'))] }));
    const expiry = vi
      .fn()
      .mockResolvedValueOnce(chainExpiry())
      .mockResolvedValue(chainExpiry({ expiry_listed: false, tag: null, calls: side('call', [], null), puts: side('put', [], null) }));
    const service = fakeChainService(vi, { day, expiry });
    await renderChainTab(service);
    await openStandardChain();

    pickDate('2026-10-12');

    await waitFor(() => expect(paragraph(/expiry is not listed on this date/)).toBeTruthy());
    expect(reads(paragraph(/expiry is not listed on this date/))).toContain('The 2026-10-16 expiry is not listed on this date.');
    expect(reads(within(expiriesTable()).getAllByRole('rowheader')[0] ?? null)).toBe('2026-10-30');
    expect(within(expiriesTable()).getAllByRole('button', { name: /Show chain/ }).every((button) => button.getAttribute('aria-pressed') === 'false')).toBe(true);
    expect(expiry).toHaveBeenLastCalledWith(expect.objectContaining({ expiryMs: EXPIRY_MS['2026-10-16'] }));
  });
});

function expiryRow16(): HTMLElement {
  const row = within(expiriesTable()).getByRole('rowheader', { name: '2026-10-16' }).closest('tr');
  if (row === null) throw new Error('no row');
  return row;
}

describe('a chain that cannot be had', () => {
  it('says so in the server words, with its code, and tries again on request', async () => {
    const expiry = vi
      .fn()
      .mockRejectedValueOnce(new PastChainRefusedError('OPTIONS_BACKTEST_LAKE_CATALOG_UNAVAILABLE', "The data lake's catalog cannot be reached, so what is stored cannot be read.", 503))
      .mockResolvedValue(chainExpiry());
    await renderChainTab(fakeChainService(vi, { expiry }));
    pickDate(TRADING_DATE);
    await screen.findByRole('table', { name: /Every expiry the listing held/ });
    showChain('2026-10-16');

    expect((await screen.findAllByRole('alert')).some((alert) => alert.textContent?.includes("The data lake's catalog cannot be reached"))).toBe(true);
    screen.getByRole('button', { name: 'Try again' }).click();

    expect(await screen.findByRole('table', { name: /Calls on the left/ })).toBeTruthy();
    expect(expiry).toHaveBeenCalledTimes(2);
  });
});

describe('every date and every time of the tab', () => {
  it('is New York time on the chain: the expiry, the instant it is priced at, and the time of each trade', async () => {
    await renderChainTab(fakeChainService(vi));
    await openStandardChain();

    // The head's date, the instant, the underlying's time, three trades, the caption's date and the expiry rows: all of them.
    expect(assertNewYorkTimes(document.body)).toBeGreaterThanOrEqual(11);
    expect(screen.getByRole('table', { name: /for the 2026-10-16 expiry\.$/ })).toBeTruthy();
  });

  it('is New York time where an expiry is not listed, and where whether it is cannot be said', async () => {
    const service = fakeChainService(vi, { expiry: vi.fn(async () => chainExpiry({ expiry_listed: false, tag: null, calls: side('call', [], null), puts: side('put', [], null) })) });
    await renderChainTab(service);
    pickDate(TRADING_DATE);
    await screen.findByRole('table', { name: /Every expiry the listing held/ });
    showChain('2026-10-16');
    await waitFor(() => expect(paragraph(/expiry is not listed on this date/)).toBeTruthy());

    expect(assertNewYorkTimes(paragraph(/expiry is not listed on this date/))).toBe(1);
  });

  it('names the date of the expiry to open in the name of its button, so that Show chain is not said twenty times without one', async () => {
    await renderChainTab(fakeChainService(vi));
    pickDate(TRADING_DATE);
    await screen.findByRole('table', { name: /Every expiry the listing held/ });

    const names = within(expiriesTable()).getAllByRole('button').map((button) => button.getAttribute('aria-labelledby'));

    expect(names).toHaveLength(3);
    expect(within(expiriesTable()).getByRole('button', { name: '2026-10-16 Show chain' })).toBeTruthy();
    expect(within(expiriesTable()).getByRole('button', { name: '2026-10-30 Show chain' })).toBeTruthy();
  });
});
