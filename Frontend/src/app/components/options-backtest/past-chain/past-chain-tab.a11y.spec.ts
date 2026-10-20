import { fireEvent, screen } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it, vi } from 'vitest';

import { PastChainInvalidError, PastChainRefusedError, DownloadNotAllowedError } from './past-chain.service';
import { deferred } from './testing/deferred';
import { chainDay, chainExpiry, contract, downloadAnswer, item, side, sideWithoutMoney, underlyingPrice } from './testing/fixtures';
import { fakeChainService, openChainCard, openStandardChain, pickDate, renderChainTab, showChain, typeStrikes, typeTime } from './testing/render-chain';

async function violations(): Promise<string[]> {
  const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });
  return results.violations.map((violation) => `${violation.id}: ${violation.nodes.map((node) => node.html).join(' | ')}`);
}

describe('the Past chain tab for a screen reader', () => {
  it('has no violation before a date is picked, and names every control', async () => {
    await renderChainTab(fakeChainService(vi));

    expect.soft(await violations(), 'has no violation before a date is picked').toEqual([]);
    for (const name of ['Trading date', 'Time of day (New York time)', 'Strikes each side of the money']) {
      expect.soft(screen.getByLabelText(name), `names the ${name} control`).toBeTruthy();
    }
    expect.soft(screen.getByRole('combobox', { name: /chain underlying/i }), 'names the chain underlying control').toBeTruthy();
  });

  it('has no violation with the expiries listed, a chain open in every state a contract can be in, and a download on offer', async () => {
    const odd = contract({ right: 'call', thousandths: 604_000, state: 'did_not_trade', standard: false });
    const service = fakeChainService(vi, {
      expiry: vi.fn(async () => chainExpiry({ calls: side('call', [...chainExpiry().calls.contracts, odd]) })),
    });
    await renderChainTab(service);

    await openStandardChain();

    expect(await violations()).toEqual([]);
  });

  it('makes each table that scrolls a named region the keyboard reaches', async () => {
    await renderChainTab(fakeChainService(vi));
    await openStandardChain();

    for (const name of ['Scrollable list of expiries', 'Scrollable chain table']) {
      expect(screen.getByRole('region', { name }).getAttribute('tabindex')).toBe('0');
    }
  });

  it('has no violation when no strikes are drawn because the underlying has no price', async () => {
    const chain = chainExpiry({
      calls: sideWithoutMoney('call', 120, 118),
      puts: sideWithoutMoney('put', 118, 116),
      underlying_price: underlyingPrice({ state: 'not_stored', price: null, price_time_ms: null }),
    });
    await renderChainTab(fakeChainService(vi, { expiry: vi.fn(async () => chain) }));
    await openChainCard();

    expect(await violations()).toEqual([]);
  });

  it('has no violation with two contracts at one strike, an empty cell for each reason, and a state it does not know', async () => {
    const odd = { ...contract({ right: 'call', thousandths: 602_000, state: 'did_not_trade', standard: false }), symbol: 'SPY1261016C00602000' };
    const unknown = { ...contract({ right: 'call', thousandths: 604_000, state: 'traded' }), price_state: 'halted' as 'traded' };
    const chain = chainExpiry({
      calls: side('call', [contract({ right: 'call', thousandths: 602_000, state: 'did_not_trade' }), odd, unknown], 602_000, { listed: 90 }),
      puts: side('put', [contract({ right: 'put', thousandths: 601_000, state: 'did_not_trade' }), contract({ right: 'put', thousandths: 602_000, state: 'did_not_trade' })], 602_000, { listed: 80 }),
    });
    await renderChainTab(fakeChainService(vi, { expiry: vi.fn(async () => chain) }));
    await openStandardChain();

    expect(await violations()).toEqual([]);
  });

  it('has no violation while a listing is read again after a download, with the tables still on screen', async () => {
    const pending = deferred<ReturnType<typeof chainDay>>();
    const day = vi.fn().mockResolvedValueOnce(chainDay()).mockReturnValueOnce(pending.promise);
    await renderChainTab(fakeChainService(vi, { day, download: vi.fn(async () => downloadAnswer([item('contract', 'SPY261016P00601000', 'fetched')])) }));
    await openStandardChain();
    fireEvent.click(screen.getByRole('button', { name: 'Download missing data' }));
    await screen.findAllByText('Reading again. What is shown is the last answer.');

    expect(await violations()).toEqual([]);
    pending.resolve(chainDay());
  });

  it('has no violation while a read is going', async () => {
    const pending = deferred<ReturnType<typeof chainDay>>();
    await renderChainTab(fakeChainService(vi, { day: vi.fn(() => pending.promise) }));

    pickDate('2026-10-09');
    await screen.findByText('Reading the listing…');

    expect(await violations()).toEqual([]);
    pending.resolve(chainDay());
  });

  it.each([
    [
      'a listing that is not stored, with the download on offer',
      () => vi.fn(async () => chainDay({ listing_state: 'not_stored', expiries: [] })),
      '2026-10-09',
      /listing for this date|stored listing/,
    ],
    [
      'a listing that cannot be read',
      () => vi.fn(async () => chainDay({ listing_state: 'unreadable', listing_problem: 'cannot be read', expiries: [] })),
      '2026-10-09',
      /listing for this date|stored listing/,
    ],
    [
      'a listing that holds nothing usable',
      () => vi.fn(async () => chainDay({ expiries: [], rows_left_out_unusable: 3 })),
      '2026-10-09',
      /listing for this date|stored listing/,
    ],
    [
      'a date that is not a trading day',
      () =>
        vi.fn(async () => {
          throw new PastChainRefusedError('OPTIONS_BACKTEST_NOT_A_SESSION', '2026-10-10 is a weekend day', 400);
        }),
      '2026-10-10',
      'Not a trading day.',
    ],
    [
      'a listing that fails to be read, and for an underlying or date the server refused',
      () =>
        vi.fn(async () => {
          throw new PastChainInvalidError([{ loc: ['query', 'trading_date_ms'], msg: 'Input should be greater than or equal to 0' }]);
        }),
      '1969-12-31',
      /The server did not take the trading date/,
    ],
    [
      'a session the lake will fetch nothing for yet',
      () => vi.fn(async () => chainDay({ listing_state: 'not_stored', expiries: [], fetch_refusal: { reason: 'session_not_over', words: 'The session is not over in New York yet.' } })),
      '2026-10-09',
      'The session is not over in New York yet.',
    ],
  ])('has no violation for %s', async (_name, day, date, shown) => {
    await renderChainTab(fakeChainService(vi, { day: day() }));

    pickDate(date);
    await screen.findByText(shown);

    expect(await violations()).toEqual([]);
  });

  it('has no violation with a time and strikes the page refuses, or a chain that waits on them', async () => {
    await renderChainTab(fakeChainService(vi));
    await openStandardChain();

    typeTime('09:00');
    typeStrikes('x');
    await screen.findByText(/The chain is not read while/);

    expect(await violations()).toEqual([]);
  });

  it.each([
    [
      'an expiry that is not listed',
      () => vi.fn(async () => chainExpiry({ expiry_listed: false, tag: null, calls: side('call', [], null), puts: side('put', [], null) })),
      /expiry is (not listed|listed cannot)|Whether the/,
    ],
    [
      'an expiry whose listing is not stored',
      () => vi.fn(async () => chainExpiry({ expiry_listed: null, tag: null, listing_state: 'not_stored', calls: side('call', [], null), puts: side('put', [], null) })),
      /expiry is (not listed|listed cannot)|Whether the/,
    ],
    [
      'a chain that failed to be read',
      () =>
        vi.fn(async () => {
          throw new PastChainRefusedError('OPTIONS_BACKTEST_LAKE_CATALOG_UNAVAILABLE', 'The catalog cannot be reached.', 503);
        }),
      'The catalog cannot be reached.',
    ],
  ])('has no violation for %s', async (_name, expiry, shown) => {
    await renderChainTab(fakeChainService(vi, { expiry: expiry() }));
    pickDate('2026-10-09');
    await screen.findByRole('table', { name: /Every expiry the listing held/ });
    showChain('2026-10-16');
    await screen.findByText(shown);

    expect(await violations()).toEqual([]);
  });

  it('has no violation with a download answered, its items failed and fetched, and the button busy while it ran', async () => {
    const answer = deferred<ReturnType<typeof downloadAnswer>>();
    const service = fakeChainService(vi, { download: vi.fn(() => answer.promise) });
    await renderChainTab(service);
    await openStandardChain();

    fireEvent.click(screen.getByRole('button', { name: 'Download missing data' }));
    await screen.findByText(/^Downloading/);
    expect(await violations()).toEqual([]);
    answer.resolve(downloadAnswer([item('listing', 'SPY', 'already_stored'), item('contract', 'SPY261016P00601000', 'failed', 'lease_timeout', 'another request holds it')]));
    await screen.findByRole('group', { name: 'Download result' });

    expect(await violations()).toEqual([]);
  });

  it('has no violation with a download the page is not allowed to make', async () => {
    const service = fakeChainService(vi, {
      download: vi.fn(async () => {
        throw new DownloadNotAllowedError('refused', 403);
      }),
    });
    await renderChainTab(service);
    await openStandardChain();

    fireEvent.click(screen.getByRole('button', { name: 'Download missing data' }));
    await screen.findByText('This page is not allowed to start a download here.');

    expect(await violations()).toEqual([]);
  });
});
