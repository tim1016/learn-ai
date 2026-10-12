import { screen, waitFor, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { reads } from '../testing/page-actions';
import { PastChainRefusedError, type DayQuery, type ExpiryQuery } from './past-chain.service';
import type { ChainDay, ChainExpiry } from './past-chain.types';
import { deferred } from './testing/deferred';
import { chainDay, chainExpiry, contract, EXPIRY_MS, MINUTE_1545_MS, side, summary, tag, TRADING_DATE_MS } from './testing/fixtures';
import { expiriesTable, fakeChainService, openStandardChain, pickDate, renderChainTab, showChain, typeTime } from './testing/render-chain';

/** A chain whose one call traded at this price, so that a spec can tell which answer is on screen. */
const chainAt = (price: string, changes: Partial<ChainExpiry> = {}) =>
  chainExpiry({ calls: side('call', [contract({ right: 'call', thousandths: 602_000, state: 'traded', price })]), puts: side('put', []), ...changes });

const shownPrices = () => [...document.querySelectorAll('app-options-backtest-chain-contract .price')].map((price) => reads(price));
const expiryDates = () => within(expiriesTable()).getAllByRole('rowheader').map((header) => reads(header));

/** The day of the listing for one expiry only, so that a spec can tell which day's answer is on screen. */
const dayWith = (date: keyof typeof EXPIRY_MS): ChainDay => chainDay({ expiries: [summary(EXPIRY_MS[date], tag('weekly'))] });

describe('an answer that arrives after the owner has asked for something else', () => {
  it('is not the listing on screen: the listing of the earlier date, arriving last, is dropped', async () => {
    const first = deferred<ChainDay>();
    const day = vi.fn((query: DayQuery) => (query.tradingDateMs === TRADING_DATE_MS ? first.promise : Promise.resolve(dayWith('2026-10-30'))));
    await renderChainTab(fakeChainService(vi, { day }));

    pickDate('2026-10-09');
    pickDate('2026-10-12');
    await screen.findByRole('table', { name: /Every expiry the listing held/ });
    expect(expiryDates()).toEqual(['2026-10-30']);
    first.resolve(dayWith('2026-10-16'));
    await first.promise;
    await new Promise((done) => setTimeout(done, 0));

    expect(expiryDates()).toEqual(['2026-10-30']);
    expect(day).toHaveBeenCalledTimes(2);
  });

  it('is not shown while the new date is still being read: the old listing goes at once and is not put back', async () => {
    const second = deferred<ChainDay>();
    const day = vi.fn((query: DayQuery) => (query.tradingDateMs === TRADING_DATE_MS ? Promise.resolve(dayWith('2026-10-16')) : second.promise));
    await renderChainTab(fakeChainService(vi, { day }));
    pickDate('2026-10-09');
    await screen.findByRole('table', { name: /Every expiry the listing held/ });

    pickDate('2026-10-12');

    await screen.findByText('Reading the listing…');
    expect(screen.queryByRole('table', { name: /Every expiry the listing held/ })).toBeNull();
    second.resolve(dayWith('2026-10-30'));
    await screen.findByRole('table', { name: /Every expiry the listing held/ });
    expect(expiryDates()).toEqual(['2026-10-30']);
  });

  it('is not the chain on screen: the chain of the earlier expiry, arriving last, is dropped', async () => {
    const first = deferred<ChainExpiry>();
    const expiry = vi.fn((query: ExpiryQuery) => (query.expiryMs === EXPIRY_MS['2026-10-16'] ? first.promise : Promise.resolve(chainAt('7.77'))));
    await renderChainTab(fakeChainService(vi, { expiry }));
    pickDate('2026-10-09');
    await screen.findByRole('table', { name: /Every expiry the listing held/ });

    showChain('2026-10-16');
    showChain('2026-10-30');
    await screen.findByRole('table', { name: /Calls on the left/ });
    first.resolve(chainAt('1.11'));
    await first.promise;
    await new Promise((done) => setTimeout(done, 0));

    expect(shownPrices()).toEqual(['$7.77']);
  });

  it('is not used to read a chain: the listing of an earlier date, arriving last, does not bring the chain of that date', async () => {
    // 2026-10-12 is slow and 2026-10-13 is not; New York midnights 1_791_777_600_000 and 1_791_864_000_000 (04:00 UTC).
    const slowDay = deferred<ChainDay>();
    const day = vi.fn((query: DayQuery) => (query.tradingDateMs === 1_791_777_600_000 ? slowDay.promise : Promise.resolve(chainDay())));
    const expiry = vi.fn(async (query: ExpiryQuery) => chainAt(query.tradingDateMs === 1_791_777_600_000 ? '2.22' : '1.11'));
    await renderChainTab(fakeChainService(vi, { day, expiry }));
    pickDate('2026-10-09');
    await screen.findByRole('table', { name: /Every expiry the listing held/ });
    showChain('2026-10-16');
    await waitFor(() => expect(shownPrices()).toEqual(['$1.11']));

    pickDate('2026-10-12');
    pickDate('2026-10-13');
    await waitFor(() => expect(expiry.mock.lastCall?.[0].tradingDateMs).toBe(1_791_864_000_000));
    slowDay.resolve(chainDay());
    await slowDay.promise;
    await new Promise((done) => setTimeout(done, 0));

    expect(expiry.mock.calls.map((call) => call[0].tradingDateMs)).toEqual([TRADING_DATE_MS, 1_791_864_000_000]);
    expect(shownPrices()).toEqual(['$1.11']);
  });

  it('is not the chain on screen: the chain of an earlier time of day, arriving last, is dropped', async () => {
    const first = deferred<ChainExpiry>();
    const expiry = vi.fn((query: ExpiryQuery) => {
      if (query.atMs === null) return Promise.resolve(chainAt('1.11'));
      return query.atMs === MINUTE_1545_MS ? first.promise : Promise.resolve(chainAt('3.33'));
    });
    await renderChainTab(fakeChainService(vi, { expiry }));
    await openStandardChain();

    typeTime('15:45');
    typeTime('15:46');
    await waitFor(() => expect(shownPrices()).toEqual(['$3.33']));
    first.resolve(chainAt('2.22'));
    await first.promise;
    await new Promise((done) => setTimeout(done, 0));

    expect(shownPrices()).toEqual(['$3.33']);
  });

  it('is not an error on screen: a failure of the earlier read, arriving last, says nothing', async () => {
    const first = deferred<ChainExpiry>();
    const expiry = vi.fn((query: ExpiryQuery) => (query.expiryMs === EXPIRY_MS['2026-10-16'] ? first.promise : Promise.resolve(chainAt('7.77'))));
    await renderChainTab(fakeChainService(vi, { expiry }));
    pickDate('2026-10-09');
    await screen.findByRole('table', { name: /Every expiry the listing held/ });
    showChain('2026-10-16');
    showChain('2026-10-30');
    await screen.findByRole('table', { name: /Calls on the left/ });

    first.reject(new PastChainRefusedError('OPTIONS_BACKTEST_LAKE_CATALOG_UNAVAILABLE', 'The data lake catalog cannot be reached.', 503));
    await first.promise.catch(() => undefined);
    await new Promise((done) => setTimeout(done, 0));

    expect(screen.queryByRole('alert')).toBeNull();
    expect(shownPrices()).toEqual(['$7.77']);
  });

  it('is shown with the date it was asked for, and not as an answer about the date on screen', async () => {
    const answer = deferred<import('./past-chain.types').DownloadAnswer>();
    const day = vi.fn(async () => chainDay({ listing_state: 'not_stored', expiries: [] }));
    const service = fakeChainService(vi, { day, download: vi.fn(() => answer.promise) });
    await renderChainTab(service);
    pickDate('2026-10-09');
    (await screen.findByRole('button', { name: 'Download missing data' })).click();

    pickDate('2026-10-12');
    answer.resolve({ underlying: 'SPY', session_open_ms: 1_791_552_600_000, session_close_ms: 1_791_576_000_000, items: [{ kind: 'listing', subject: 'SPY', outcome: 'fetched', reason: null, detail: null }] });
    await answer.promise;

    // The answer arrives for 2026-10-09, while 2026-10-12 is on screen: it says whose it is.
    await screen.findByRole('group', { name: 'Download result' });
    expect(reads(screen.getByRole('heading', { name: /Download of/ }))).toBe('Download of SPY on 2026-10-09');
  });
});
