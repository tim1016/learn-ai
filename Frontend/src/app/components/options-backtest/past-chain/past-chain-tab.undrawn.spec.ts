import { screen, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { paragraph, reads } from '../testing/page-actions';
import type { ChainExpiry, UnderlyingPrice } from './past-chain.types';
import { chainDay, chainExpiry, contract, SESSION_OPEN_MS, side, sideWithoutMoney, underlyingPrice } from './testing/fixtures';
import { chainTable, fakeChainService, openChainCard, openStandardChain, pickDate, renderChainTab } from './testing/render-chain';

/** What the server sends when the underlying has no price at the instant: no money strike, no contract on either side, and the counts it still knows. */
const withoutMoney = (price: Partial<UnderlyingPrice>, changes: Partial<ChainExpiry> = {}) =>
  chainExpiry({ calls: sideWithoutMoney('call', 120, 118), puts: sideWithoutMoney('put', 118, 116), underlying_price: underlyingPrice({ price: null, price_time_ms: null, ...price }), ...changes });

async function renderUndrawn(chain: ChainExpiry) {
  const service = fakeChainService(vi, { expiry: vi.fn(async () => chain) });
  await renderChainTab(service);
  await openChainCard();
  return service;
}

/** The chain card, as it reads. */
const card = () => reads(document.querySelector('#ob-chain-title')?.closest('section') ?? null);

/** The sentences that would say a contract is missing when it is the data that is. */
const FALSE_SENTENCES = [/No contract is listed/, /Not listed at this strike/, /Nothing in this window/];

describe('a chain whose underlying has no price at that time', () => {
  it.each([
    ['no bar has closed yet', { state: 'no_bar_yet' as const }, 'No bar of the underlying had closed by this time, so there is no price to centre them on.'],
    ['its bars for the date are not stored', { state: 'not_stored' as const }, "The underlying's bars for this date are not stored, so there is no price to centre them on."],
    ['its bars for the date cannot be read', { state: 'unreadable' as const, problem: 'the bars of SPY cannot be read (UnderlyingDayUnreadable)' }, "The underlying's bars for this date cannot be read, so there is no price to centre them on."],
  ])('says why no strikes are drawn when %s, with the listed counts as served, and never that a contract is not listed', async (_name, price, why) => {
    await renderUndrawn(withoutMoney(price));

    expect(card()).toContain('No strikes are drawn.');
    expect(card()).toContain(why);
    expect(card()).toContain('120 calls and 118 puts are listed.');
    expect(screen.queryByRole('table', { name: /Calls on the left/ })).toBeNull();
    for (const sentence of FALSE_SENTENCES) expect(card()).not.toMatch(sentence);
  });

  it('keeps the counts line as the server counted them', async () => {
    await renderUndrawn(withoutMoney({ state: 'no_bar_yet' }));

    expect(screen.getByText('Calls: 120 listed, 118 standard. Puts: 118 listed, 116 standard.')).toBeTruthy();
  });

  it('offers a later time when the session runs on past the time asked for, and not when it was asked at the close', async () => {
    await renderUndrawn(withoutMoney({ state: 'no_bar_yet' }, { at_ms: SESSION_OPEN_MS }));
    expect(card()).toContain('Pick a later time to see them.');
  });

  it('offers no later time at the close of the session', async () => {
    await renderUndrawn(withoutMoney({ state: 'no_bar_yet' }));

    expect(card()).not.toContain('Pick a later time');
  });

  it('points to the download when the bars are not stored, and the card above offers to fetch them', async () => {
    await renderUndrawn(withoutMoney({ state: 'not_stored' }));

    expect(card()).toContain('Download missing data above fetches them.');
    expect(reads(paragraph(/^Fetches/))).toBe("Fetches the underlying's bars.");
    expect(screen.getByRole('button', { name: 'Download missing data' })).toBeTruthy();
  });

  it('offers no download for bars that cannot be read', async () => {
    await renderUndrawn(withoutMoney({ state: 'unreadable', problem: 'damaged' }));

    expect(card()).not.toContain('Download missing data');
    expect(screen.queryByRole('button', { name: 'Download missing data' })).toBeNull();
  });

  it('does not point to a download while the lake will fetch nothing for the session, which the day’s answer says', async () => {
    const chain = withoutMoney({ state: 'not_stored' });
    const refusal = { reason: 'session_not_over', words: 'The session of 2026-10-09 is not over in New York yet.' } as const;
    const service = fakeChainService(vi, { expiry: vi.fn(async () => chain), day: vi.fn(async () => chainDay({ fetch_refusal: refusal })) });
    await renderChainTab(service);

    await openChainCard();

    expect(card()).toContain('No strikes are drawn.');
    expect(card()).not.toContain('Download missing data above');
    expect(screen.queryByRole('button', { name: 'Download missing data' })).toBeNull();
    expect(screen.getByText(refusal.words)).toBeTruthy();
  });

  it('says the code of an underlying state it does not know, in words, and not that the contracts are missing', async () => {
    await renderUndrawn(withoutMoney({ state: 'stale' as UnderlyingPrice['state'] }));

    expect(card()).toContain('This page does not know this state of the underlying\'s price, Stale, so it cannot say why there is no price to centre them on.');
    for (const sentence of FALSE_SENTENCES) expect(card()).not.toMatch(sentence);
  });

  it('says so too when the server calls the underlying priced and sends no price', async () => {
    await renderUndrawn(withoutMoney({ state: 'priced', price_time_ms: SESSION_OPEN_MS }));

    expect(card()).toContain('The server sent an incomplete price for the underlying, so there is no price to centre them on.');
    for (const sentence of FALSE_SENTENCES) expect(card()).not.toMatch(sentence);
  });

  it('says one call and one put in the singular', async () => {
    await renderUndrawn(withoutMoney({ state: 'no_bar_yet' }, { calls: sideWithoutMoney('call', 1, 1), puts: sideWithoutMoney('put', 1, 1) }));

    expect(card()).toContain('1 call and 1 put are listed.');
  });
});

describe('a chain with a price whose strikes cannot be centred', () => {
  it('says that a side has no standard contract to centre on, and not that nothing is listed', async () => {
    await renderUndrawn(chainExpiry({ calls: sideWithoutMoney('call', 4, 0), puts: sideWithoutMoney('put', 4, 0) }));

    expect(card()).toContain('No strikes are drawn.');
    expect(card()).toContain('The underlying has a price, but the server found no standard contract on a side to centre them on.');
    expect(card()).toContain('4 calls and 4 puts are listed.');
    for (const sentence of FALSE_SENTENCES) expect(card()).not.toMatch(sentence);
  });

  it('says only that the window is empty when the money is there and the window holds nothing', async () => {
    await renderUndrawn(chainExpiry({ calls: side('call', [], 602_000, { listed: 4 }), puts: side('put', [], 602_000, { listed: 4 }) }));

    expect(card()).toContain('The server sent no contract in this window.');
    expect(card()).toContain('4 calls and 4 puts are listed.');
  });
});

describe('a chain in which nothing is listed', () => {
  it('is the one case that says nothing is listed, because both counts say so', async () => {
    await renderUndrawn(chainExpiry({ calls: side('call', [], null), puts: side('put', [], null) }));

    expect(card()).toContain('No contract is listed at this expiry: the server counts 0 calls and 0 puts.');
    expect(card()).not.toContain('No strikes are drawn');
    expect(screen.queryByRole('table', { name: /Calls on the left/ })).toBeNull();
  });
});

describe('a chain in which one side shows nothing', () => {
  const putsOnly = () =>
    chainExpiry({
      calls: sideWithoutMoney('call', 6, 0),
      puts: side('put', [contract({ right: 'put', thousandths: 601_000, state: 'did_not_trade' }), contract({ right: 'put', thousandths: 602_000, state: 'did_not_trade' })]),
    });

  it('says which side is not drawn and why, and its cells say only that the window has nothing there', async () => {
    const service = fakeChainService(vi, { expiry: vi.fn(async () => putsOnly()) });
    await renderChainTab(service);
    await openStandardChain();

    expect(card()).toContain('No strikes are drawn for calls: the underlying has a price, but the server found no standard contract on that side to centre them on.');
    const cells = within(chainTable()).getAllByRole('row').slice(1).map((row) => reads(row.querySelectorAll('td')[0] ?? null));
    expect(cells).toEqual(['Nothing in this window', 'Nothing in this window']);
  });

  it('says a side that lists nothing is not listed at a strike, because its count says so', async () => {
    const service = fakeChainService(vi, {
      expiry: vi.fn(async () => chainExpiry({ calls: side('call', [], null), puts: side('put', [contract({ right: 'put', thousandths: 602_000, state: 'did_not_trade' })]) })),
    });
    await renderChainTab(service);
    await openStandardChain();

    const cells = within(chainTable()).getAllByRole('row').slice(1).map((row) => reads(row.querySelectorAll('td')[0] ?? null));
    expect(cells).toEqual(['Not listed at this strike']);
    expect(card()).not.toContain('No strikes are drawn for');
  });
});

describe('the Past chain tab with the day picked and nothing opened', () => {
  it('keeps the table off the screen until an expiry is open', async () => {
    await renderChainTab(fakeChainService(vi));
    pickDate('2026-10-09');
    await screen.findByRole('table', { name: /Every expiry the listing held/ });

    expect(screen.queryByText(/No strikes are drawn/)).toBeNull();
  });
});
