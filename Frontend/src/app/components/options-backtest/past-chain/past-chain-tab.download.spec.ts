import { fireEvent, screen, waitFor, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { paragraph, reads } from '../testing/page-actions';
import { DownloadNotAllowedError, PastChainRefusedError } from './past-chain.service';
import type { DownloadAnswer, DownloadRequest } from './past-chain.types';
import { deferred } from './testing/deferred';
import { chainDay, chainExpiry, contract, downloadAnswer, EXPIRY_MS, item, side, TRADING_DATE, TRADING_DATE_MS, underlyingPrice } from './testing/fixtures';
import { assertNewYorkTimes, fakeChainService, openStandardChain, pickDate, renderChainTab } from './testing/render-chain';

const button = () => screen.getByRole<HTMLButtonElement>('button', { name: 'Download missing data' });
const call = (thousandths: number, state: Parameters<typeof contract>[0]['state'], standard = true) => contract({ right: 'call', thousandths, state, standard });
const put = (thousandths: number, state: Parameters<typeof contract>[0]['state']) => contract({ right: 'put', thousandths, state });

/** A chain with every stored state in it and five contracts whose prices are not stored. */
function chainWithFiveMissing() {
  return chainExpiry({
    calls: side('call', [call(601_000, 'traded'), call(602_000, 'not_stored'), call(603_000, 'not_stored'), call(604_000, 'unreadable'), call(605_000, 'did_not_trade'), call(606_000, 'not_stored', false)]),
    puts: side('put', [put(601_000, 'not_stored'), put(602_000, 'traded'), put(603_000, 'not_stored')]),
  });
}

async function openChainOf(chain = chainWithFiveMissing(), downloadAnswerFor: (request: DownloadRequest) => Promise<DownloadAnswer> = async () => downloadAnswer([])) {
  const service = fakeChainService(vi, { expiry: vi.fn(async () => chain), download: vi.fn(downloadAnswerFor) });
  await renderChainTab(service);
  await openStandardChain();
  return service;
}

describe('what the download offers', () => {
  it('is nothing when nothing on screen is missing', async () => {
    const stored = chainExpiry({ calls: side('call', [call(602_000, 'traded'), call(603_000, 'did_not_trade')]), puts: side('put', [put(602_000, 'traded')]) });
    await openChainOf(stored);

    expect(screen.queryByRole('button', { name: 'Download missing data' })).toBeNull();
    expect(screen.queryByRole('heading', { name: 'Download missing data' })).toBeNull();
  });

  it('is the listing, and the underlying bars if they are missing, when the listing is not stored', async () => {
    const service = fakeChainService(vi, { day: vi.fn(async () => chainDay({ listing_state: 'not_stored', expiries: [] })) });
    await renderChainTab(service);

    pickDate(TRADING_DATE);

    await screen.findByRole('button', { name: 'Download missing data' });
    expect(reads(paragraph(/^Fetches/))).toBe("Fetches the listing and the underlying's bars if they are missing.");
  });

  it('is exactly the contracts on screen whose prices are not stored, and says how many', async () => {
    await openChainOf();

    expect(reads(paragraph(/^Fetches/))).toBe('Fetches 5 contracts.');
  });

  it('says one contract in the singular', async () => {
    await openChainOf(chainExpiry());

    expect(reads(paragraph(/^Fetches/))).toBe('Fetches 1 contract.');
  });

  it('is the underlying bars too when the chain says they are not stored', async () => {
    await openChainOf(chainExpiry({ underlying_price: underlyingPrice({ state: 'not_stored', price: null, price_time_ms: null }) }));

    expect(reads(paragraph(/^Fetches/))).toBe("Fetches the underlying's bars and 1 contract.");
  });

  it('is the underlying bars alone when they are the only thing missing', async () => {
    const stored = chainExpiry({
      underlying_price: underlyingPrice({ state: 'not_stored', price: null, price_time_ms: null }),
      calls: side('call', [call(602_000, 'traded')]),
      puts: side('put', []),
    });
    await openChainOf(stored);

    expect(reads(paragraph(/^Fetches/))).toBe("Fetches the underlying's bars.");
  });
});

describe('the request a download makes', () => {
  it('names exactly the contracts on screen that are not stored, calls then puts, as the server named them, with the expiry only to check them against', async () => {
    const service = await openChainOf();

    fireEvent.click(button());

    await waitFor(() => expect(service.download).toHaveBeenCalledTimes(1));
    expect(service.download.mock.calls[0]?.[0]).toEqual({
      underlying: 'SPY',
      trading_date_ms: TRADING_DATE_MS,
      expiry_ms: EXPIRY_MS['2026-10-16'],
      contracts: ['SPY261016C00602000', 'SPY261016C00603000', 'SPY261016C00606000', 'SPY261016P00601000', 'SPY261016P00603000'],
    });
  });

  it('names no contract and no expiry when only the listing is missing', async () => {
    const service = fakeChainService(vi, { day: vi.fn(async () => chainDay({ listing_state: 'not_stored', expiries: [] })) });
    await renderChainTab(service);
    pickDate(TRADING_DATE);

    fireEvent.click(await screen.findByRole('button', { name: 'Download missing data' }));

    await waitFor(() => expect(service.download).toHaveBeenCalledTimes(1));
    expect(service.download.mock.calls[0]?.[0]).toEqual({ underlying: 'SPY', trading_date_ms: TRADING_DATE_MS, contracts: [] });
  });

  it('is made once, however often the button is pressed while it runs, and the button says it is busy', async () => {
    const answer = deferred<DownloadAnswer>();
    const service = await openChainOf(chainWithFiveMissing(), () => answer.promise);

    fireEvent.click(button());
    await waitFor(() => expect(button().disabled).toBe(true));
    fireEvent.click(button());
    fireEvent.click(button());

    expect(button().getAttribute('aria-busy')).toBe('true');
    expect(screen.getByText('Downloading. The server finishes what it has begun even if you leave this tab.')).toBeTruthy();
    answer.resolve(downloadAnswer([]));
    await waitFor(() => expect(screen.queryByText(/^Downloading/)).toBeNull());
    expect(service.download).toHaveBeenCalledTimes(1);
  });
});

describe('what a download answers', () => {
  const answered = () =>
    downloadAnswer([
      item('listing', 'SPY', 'already_stored'),
      item('underlying', 'SPY', 'fetched'),
      item('contract', 'SPY261016C00602000', 'fetched'),
      item('contract', 'SPY261016C00603000', 'failed', 'provider_no_data', 'the vendor holds no trades for SPY261016C00603000 on 2026-10-09'),
      item('contract', 'SPY261016C00606000', 'failed', 'lease_timeout', 'another request holds the claim'),
    ]);

  it('is every item, with its outcome as served and, for a failure, the lake reason and its own words', async () => {
    await openChainOf(chainWithFiveMissing(), async () => answered());

    fireEvent.click(button());

    const result = await screen.findByRole('group', { name: 'Download result' });
    const lines = within(result).getAllByRole('listitem').map((line) => reads(line));
    expect(lines).toEqual([
      'Listing SPY Already Stored',
      "Underlying's bars SPY Fetched",
      'Contract SPY261016C00602000 Fetched',
      'Contract SPY261016C00603000 Failed Provider No Data the vendor holds no trades for SPY261016C00603000 on 2026-10-09',
      'Contract SPY261016C00606000 Failed Lease Timeout Being fetched by another request. Try again shortly. another request holds the claim',
    ]);
    expect(reads(paragraph(/fetched, .* already stored/))).toBe('2 fetched, 1 already stored, 2 failed.');
  });

  it('says whose it is: the underlying and the trading date it was asked for', async () => {
    await openChainOf(chainWithFiveMissing(), async () => answered());

    fireEvent.click(button());

    expect(reads(await screen.findByRole('heading', { name: /Download of/ }))).toBe('Download of SPY on 2026-10-09');
  });

  it('names the date in New York terms, never in the viewer', async () => {
    await openChainOf(chainWithFiveMissing(), async () => answered());

    fireEvent.click(button());

    const result = await screen.findByRole('group', { name: 'Download result' });
    expect(assertNewYorkTimes(result)).toBe(1);
  });

  it('moves the keyboard to the result, where the pressed button no longer is', async () => {
    await openChainOf(chainWithFiveMissing(), async () => answered());

    fireEvent.click(button());

    const result = await screen.findByRole('group', { name: 'Download result' });
    await waitFor(() => expect(document.activeElement).toBe(result));
  });

  it('is followed by reading the listing and the chain again, which now show what was fetched', async () => {
    const day = vi.fn(async () => chainDay());
    const expiry = vi
      .fn()
      .mockResolvedValueOnce(chainWithFiveMissing())
      .mockResolvedValue(chainExpiry({ calls: side('call', [call(602_000, 'traded'), call(603_000, 'traded')]), puts: side('put', [put(601_000, 'traded')]) }));
    const service = fakeChainService(vi, { day, expiry, download: vi.fn(async () => answered()) });
    await renderChainTab(service);
    await openStandardChain();

    fireEvent.click(button());

    await waitFor(() => expect(day).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(expiry).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Download missing data' })).toBeNull());
    expect(chainTableOf().querySelectorAll('app-options-backtest-chain-contract .price').length).toBe(3);
    // The answer stays, so the owner can still read what the click did.
    expect(screen.getByRole('group', { name: 'Download result' })).toBeTruthy();
  });

  it('is dropped when the owner picks another date, which it was not about', async () => {
    await openChainOf(chainWithFiveMissing(), async () => answered());
    fireEvent.click(button());
    await screen.findByRole('group', { name: 'Download result' });

    pickDate('2026-10-12');

    await waitFor(() => expect(screen.queryByRole('group', { name: 'Download result' })).toBeNull());
  });
});

function chainTableOf(): HTMLElement {
  return screen.getByRole('table', { name: /Calls on the left/ });
}

describe('a download the server will not take', () => {
  it('names the server cap in the server words when it refuses a call for naming too many, sends nothing else, and leaves the button free', async () => {
    const refusal = new PastChainRefusedError('OPTIONS_BACKTEST_TOO_MANY_CONTRACTS', 'a download takes at most 60 contracts; 61 were named', 400);
    const service = await openChainOf(chainWithFiveMissing(), async () => {
      throw refusal;
    });

    fireEvent.click(button());

    expect((await screen.findAllByRole('alert')).map((alert) => reads(alert))).toContain('a download takes at most 60 contracts; 61 were named');
    expect(screen.getByText('Options Backtest Too Many Contracts')).toBeTruthy();
    expect(service.download).toHaveBeenCalledTimes(1);
    await new Promise((done) => setTimeout(done, 20));
    expect(service.download).toHaveBeenCalledTimes(1);
    expect(button().disabled).toBe(false);
    // Nothing was fetched, so nothing is read again.
    expect(service.day).toHaveBeenCalledTimes(1);
  });

  it.each([401, 403])('says this page is not allowed to start a download here when the data plane answers %i, and does not call it a failure of the download', async (status) => {
    await openChainOf(chainWithFiveMissing(), async () => {
      throw new DownloadNotAllowedError('The presented data-plane control secret does not match.', status);
    });

    fireEvent.click(button());

    const alert = await screen.findByRole('alert');
    expect(reads(alert)).toBe('This page is not allowed to start a download here.');
    expect(screen.getByText('The presented data-plane control secret does not match.')).toBeTruthy();
    expect(screen.queryByText(/could not be started/)).toBeNull();
  });

  it('says what the browser says when the call never reached the server, and that nothing was downloaded', async () => {
    await openChainOf(chainWithFiveMissing(), async () => {
      throw new Error('network down');
    });

    fireEvent.click(button());

    expect(reads(await screen.findByRole('alert'))).toBe('The download could not be started.');
    expect(screen.getByText('Nothing was downloaded. You can press the button again.')).toBeTruthy();
  });
});

describe('a session that is not over yet', () => {
  const notOver = () =>
    downloadAnswer([
      item('listing', 'SPY', 'failed', 'session_not_over', '2026-10-09 is not over in New York yet; its option data is taken from New York midnight after it, 1791604800000, and it is 1791528000000'),
      item('underlying', 'SPY', 'failed', 'session_not_over', '2026-10-09 is not over in New York yet; its option data is taken from New York midnight after it, 1791604800000, and it is 1791528000000'),
    ]);

  async function renderNotOver() {
    const day = vi.fn(async () => chainDay({ listing_state: 'not_stored', expiries: [] }));
    const service = fakeChainService(vi, { day, download: vi.fn(async () => notOver()) });
    await renderChainTab(service);
    pickDate(TRADING_DATE);
    fireEvent.click(await screen.findByRole('button', { name: 'Download missing data' }));
    await screen.findByText('The session is not over yet, so its data cannot be fetched until the next New York day.');
    return service;
  }

  it('is said in words once the lake has refused for that reason, with nothing left to press and the lake own sentence kept', async () => {
    await renderNotOver();

    expect(screen.queryByRole('button', { name: 'Download missing data' })).toBeNull();
    expect(screen.getAllByText(/is not over in New York yet/).length).toBe(2);
    expect(screen.getAllByText('Session Not Over').length).toBe(2);
  });

  it('is only said of that date: another date offers the download again', async () => {
    await renderNotOver();

    pickDate('2026-10-12');

    expect(await screen.findByRole('button', { name: 'Download missing data' })).toBeTruthy();
    expect(screen.queryByText(/The session is not over yet/)).toBeNull();
  });
});
