import { fireEvent, screen, waitFor, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { pickSymbol } from '../../../shared/symbol-picker/testing/fake-picker-world';
import { paragraph, reads } from '../testing/page-actions';
import { DownloadNotAllowedError, PastChainInvalidError, PastChainRefusedError } from './past-chain.service';
import type { ChainDay, ChainExpiry, DownloadAnswer, DownloadRequest } from './past-chain.types';
import { deferred } from './testing/deferred';
import { chainDay, chainExpiry, contract, downloadAnswer, EXPIRY_MS, item, side, TRADING_DATE, TRADING_DATE_MS, underlyingPrice } from './testing/fixtures';
import { assertNewYorkTimes, expiriesTable, fakeChainService, openStandardChain, pickDate, renderChainTab } from './testing/render-chain';

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

  it('shows the tickers of the answer through the shared symbol display, and a contract symbol as plain code', async () => {
    await openChainOf(chainWithFiveMissing(), async () => answered());

    fireEvent.click(button());

    const result = await screen.findByRole('group', { name: 'Download result' });
    const items = within(result).getAllByRole('listitem');
    expect(items.map((line) => line.querySelectorAll('app-asset-identity').length)).toEqual([1, 1, 0, 0, 0]);
    expect(items.map((line) => line.querySelectorAll('.subject').length)).toEqual([0, 0, 1, 1, 1]);
    expect(result.querySelectorAll('h3 app-asset-identity').length).toBe(1);
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

  it('keeps the listing and the chain on screen while they are read again, says so quietly, and shows what was fetched when they arrive', async () => {
    const secondDay = deferred<ChainDay>();
    const secondChain = deferred<ChainExpiry>();
    const day = vi.fn().mockResolvedValueOnce(chainDay()).mockReturnValueOnce(secondDay.promise);
    const expiry = vi.fn().mockResolvedValueOnce(chainWithFiveMissing()).mockReturnValueOnce(secondChain.promise);
    const service = fakeChainService(vi, { day, expiry, download: vi.fn(async () => answered()) });
    await renderChainTab(service);
    await openStandardChain();

    fireEvent.click(button());
    await waitFor(() => expect(day).toHaveBeenCalledTimes(2));

    expect(expiriesTable()).toBeTruthy();
    expect(chainTableOf()).toBeTruthy();
    expect(screen.queryByText('Reading the listing…')).toBeNull();
    expect(screen.queryByText('Reading the chain…')).toBeNull();
    expect(screen.getAllByText('Reading again. What is shown is the last answer.')).toHaveLength(2);
    secondDay.resolve(chainDay());
    await waitFor(() => expect(expiry).toHaveBeenCalledTimes(2));
    expect(chainTableOf()).toBeTruthy();
    expect(screen.queryByText('Reading the chain…')).toBeNull();
    expect(screen.getAllByText('Reading again. What is shown is the last answer.')).toHaveLength(1);
    secondChain.resolve(chainExpiry({ calls: side('call', [call(602_000, 'traded'), call(603_000, 'traded')]), puts: side('put', [put(601_000, 'traded')]) }));
    await waitFor(() => expect(screen.queryByText('Reading again. What is shown is the last answer.')).toBeNull());

    expect(chainTableOf().querySelectorAll('app-options-backtest-chain-contract .price').length).toBe(3);
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

describe('a download for an underlying other than the one the tab started with', () => {
  const NOT_STORED = () => chainDay({ listing_state: 'not_stored', expiries: [] });

  async function renderAfterPicking(download: (request: DownloadRequest) => Promise<DownloadAnswer>) {
    const service = fakeChainService(vi, { day: vi.fn(async () => NOT_STORED()), download: vi.fn(download) });
    const view = await renderChainTab(service);
    pickDate(TRADING_DATE);
    await screen.findByRole('button', { name: 'Download missing data' });
    pickSymbol(view.fixture, 'AAPL');
    await waitFor(() => expect(service.day).toHaveBeenCalledTimes(2));
    return { service, view };
  }

  it('asks for the underlying that was picked, and not for the one the tab started with', async () => {
    const { service } = await renderAfterPicking(async () => downloadAnswer([], { underlying: 'AAPL' }));

    fireEvent.click(await screen.findByRole('button', { name: 'Download missing data' }));

    await waitFor(() => expect(service.download).toHaveBeenCalledTimes(1));
    expect(service.download.mock.calls[0]?.[0]).toEqual({ underlying: 'AAPL', trading_date_ms: TRADING_DATE_MS, contracts: [] });
  });

  it('names the underlying the answer is about in its heading, and not the one the tab started with', async () => {
    await renderAfterPicking(async () => downloadAnswer([item('listing', 'AAPL', 'fetched'), item('underlying', 'AAPL', 'fetched')], { underlying: 'AAPL' }));

    fireEvent.click(await screen.findByRole('button', { name: 'Download missing data' }));

    expect(reads(await screen.findByRole('heading', { name: /Download of/ }))).toBe('Download of AAPL on 2026-10-09');
    expect(within(screen.getByRole('group', { name: 'Download result' })).getAllByRole('listitem').map((line) => reads(line))).toEqual(['Listing AAPL Fetched', "Underlying's bars AAPL Fetched"]);
  });

  it('puts away the answer for the earlier underlying when another is picked, so that it is never read as the answer for the new one', async () => {
    const day = vi.fn(async () => NOT_STORED());
    const service = fakeChainService(vi, { day, download: vi.fn(async () => downloadAnswer([item('listing', 'SPY', 'fetched')])) });
    const view = await renderChainTab(service);
    pickDate(TRADING_DATE);
    fireEvent.click(await screen.findByRole('button', { name: 'Download missing data' }));
    expect(reads(await screen.findByRole('heading', { name: /Download of/ }))).toBe('Download of SPY on 2026-10-09');

    pickSymbol(view.fixture, 'AAPL');

    await waitFor(() => expect(screen.queryByRole('group', { name: 'Download result' })).toBeNull());
    expect(screen.queryByRole('heading', { name: /Download of/ })).toBeNull();
    // The date was not changed, so this is the underlying's doing alone.
    expect((screen.getByLabelText('Trading date') as HTMLInputElement).value).toBe(TRADING_DATE);
  });
});

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

  const MAY_OR_MAY_NOT = 'The download may or may not have happened. The day has been read again: what is shown is what is stored.';

  it('says what the browser says when the call never came back, that it may or may not have happened, and reads the day and the chain again', async () => {
    const service = await openChainOf(chainWithFiveMissing(), async () => {
      throw new Error('network down');
    });

    fireEvent.click(button());

    expect(reads(await screen.findByRole('alert'))).toBe('The download could not be started.');
    expect(screen.getByText(MAY_OR_MAY_NOT)).toBeTruthy();
    expect(screen.queryByText(/Nothing was downloaded/)).toBeNull();
    await waitFor(() => expect(service.day).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(service.expiry).toHaveBeenCalledTimes(2));
  });

  it('says it may or may not have happened, and reads again, when the server failed (503) after it began', async () => {
    const service = await openChainOf(chainWithFiveMissing(), async () => {
      throw new PastChainRefusedError('OPTIONS_BACKTEST_LAKE_CATALOG_UNAVAILABLE', "The data lake's catalog cannot be reached.", 503);
    });

    fireEvent.click(button());

    expect(await screen.findByText(MAY_OR_MAY_NOT)).toBeTruthy();
    expect(screen.queryByText(/Nothing was downloaded/)).toBeNull();
    await waitFor(() => expect(service.day).toHaveBeenCalledTimes(2));
  });

  it.each([
    ['refuses the request', new PastChainRefusedError('OPTIONS_BACKTEST_TOO_MANY_CONTRACTS', 'too many', 400)],
    ['does not accept it (422)', new PastChainInvalidError([{ loc: ['body', 'contracts'], msg: 'Input should be a valid list' }])],
  ])('says nothing was downloaded, and reads nothing again, when the server %s', async (_name, refusal) => {
    const service = await openChainOf(chainWithFiveMissing(), async () => {
      throw refusal;
    });

    fireEvent.click(button());

    expect(await screen.findByText('Nothing was downloaded.')).toBeTruthy();
    expect(screen.queryByText(/may or may not have happened/)).toBeNull();
    await new Promise((done) => setTimeout(done, 20));
    expect(service.day).toHaveBeenCalledTimes(1);
  });
});

describe('a download that finishes after the tab is gone', () => {
  it('ends quietly: there is nothing left to read again or to put the keyboard on', async () => {
    const answer = deferred<DownloadAnswer>();
    const service = fakeChainService(vi, { day: vi.fn(async () => chainDay({ listing_state: 'not_stored', expiries: [] })), download: vi.fn(() => answer.promise) });
    const view = await renderChainTab(service);
    pickDate(TRADING_DATE);
    await screen.findByRole('button', { name: 'Download missing data' });
    // The click handler does not hand back its promise, so the spec starts the download as the button does and keeps it.
    const download: Promise<void> = view.fixture.componentInstance['startDownload']();
    await waitFor(() => expect(service.download).toHaveBeenCalledTimes(1));

    view.fixture.destroy();
    answer.resolve(downloadAnswer([item('listing', 'SPY', 'fetched')]));

    await expect(download).resolves.toBeUndefined();
    expect(service.day).toHaveBeenCalledTimes(1);
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

  it('is said when only some of the items say so, as well as when all do', async () => {
    const mixed = downloadAnswer([item('listing', 'SPY', 'fetched'), item('underlying', 'SPY', 'failed', 'session_not_over', 'not over'), item('contract', 'SPY261016C00602000', 'failed', 'provider_no_data', 'none')]);
    const day = vi.fn(async () => chainDay({ listing_state: 'not_stored', expiries: [] }));
    await renderChainTab(fakeChainService(vi, { day, download: vi.fn(async () => mixed) }));
    pickDate(TRADING_DATE);

    fireEvent.click(await screen.findByRole('button', { name: 'Download missing data' }));

    expect(await screen.findByText('The session is not over yet, so its data cannot be fetched until the next New York day.')).toBeTruthy();
  });

  it('is only said of that date: another date offers the download again', async () => {
    await renderNotOver();

    pickDate('2026-10-12');

    expect(await screen.findByRole('button', { name: 'Download missing data' })).toBeTruthy();
    expect(screen.queryByText(/The session is not over yet/)).toBeNull();
  });
});
