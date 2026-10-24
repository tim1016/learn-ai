import { fireEvent, screen, waitFor, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { deferred } from '../../../../components/options-backtest/testing/deferred';
import { SERVED_LIMITS } from '../../../../components/options-backtest/testing/fixtures';
import { paragraph, reads } from '../../../../components/options-backtest/testing/page-actions';
import type { SetupLimits } from '../../../../components/options-backtest/options-backtest.types';
import { DownloadNotAllowedError, PastChainInvalidError, PastChainRefusedError } from './past-chain.service';
import type { ChainDay, ChainExpiry, DownloadAnswer, DownloadRequest, FetchRefusal } from './past-chain.types';
import { chainDay, chainExpiry, contract, downloadAnswer, EXPIRY_MS, item, side, TRADING_DATE, TRADING_DATE_MS } from './testing/fixtures';
import { fakeChainService, openStandardChain, pickDate, renderArchive } from './testing/render-archive';

const button = () => screen.getByRole<HTMLButtonElement>('button', { name: 'Download missing data' });
/** What the card says it will fetch: the paragraph the button is described by. */
const words = () => reads(document.getElementById('chain-download-words'));
const call = (thousandths: number, state: Parameters<typeof contract>[0]['state'], standard = true) => contract({ right: 'call', thousandths, state, standard });
const put = (thousandths: number, state: Parameters<typeof contract>[0]['state']) => contract({ right: 'put', thousandths, state });
const NOT_STORED_DAY = () => chainDay({ listing_state: 'not_stored', expiries: [] });

/** A chain with every stored state in it and five contracts whose prices are not stored. */
function chainWithFiveMissing() {
  return chainExpiry({
    calls: side('call', [call(601_000, 'traded'), call(602_000, 'not_stored'), call(603_000, 'not_stored'), call(604_000, 'unreadable'), call(605_000, 'did_not_trade'), call(606_000, 'not_stored', false)]),
    puts: side('put', [put(601_000, 'not_stored'), put(602_000, 'traded'), put(603_000, 'not_stored')]),
  });
}

async function openChainOf(chain = chainWithFiveMissing(), downloadAnswerFor: (request: DownloadRequest) => Promise<DownloadAnswer> = async () => downloadAnswer([])) {
  const service = fakeChainService(vi, { expiry: vi.fn(async () => chain), download: vi.fn(downloadAnswerFor) });
  const view = await renderArchive(service);
  await openStandardChain();
  return { service, ...view };
}

describe('what the download offers', () => {
  it('is the listing, and the underlying bars if they are missing, when the listing is not stored', async () => {
    await renderArchive(fakeChainService(vi, { day: vi.fn(async () => NOT_STORED_DAY()) }));

    pickDate(TRADING_DATE);

    await screen.findByRole('button', { name: 'Download missing data' });
    expect(words()).toBe("Fetches the listing and the underlying's bars if they are missing.");
  });
});

describe('the request a download makes', () => {
  it('names exactly the contracts on screen that are not stored, calls then puts, as the server named them, with the expiry only to check them against', async () => {
    const { service } = await openChainOf();

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
    const service = fakeChainService(vi, { day: vi.fn(async () => NOT_STORED_DAY()) });
    await renderArchive(service);
    pickDate(TRADING_DATE);

    fireEvent.click(await screen.findByRole('button', { name: 'Download missing data' }));

    await waitFor(() => expect(service.download).toHaveBeenCalledTimes(1));
    expect(service.download.mock.calls[0]?.[0]).toEqual({ underlying: 'SPY', trading_date_ms: TRADING_DATE_MS, contracts: [] });
  });

  it('asks for the instrument the place is on, in capitals', async () => {
    const service = fakeChainService(vi, { day: vi.fn(async () => NOT_STORED_DAY()) });
    await renderArchive(service, undefined, { symbol: 'qqq' });
    pickDate(TRADING_DATE);

    fireEvent.click(await screen.findByRole('button', { name: 'Download missing data' }));

    await waitFor(() => expect(service.download).toHaveBeenCalledTimes(1));
    expect(service.download.mock.calls[0]?.[0]).toMatchObject({ underlying: 'QQQ' });
  });

  it('is made once, however often the button is pressed while it runs, and the button says it is busy', async () => {
    const answer = deferred<DownloadAnswer>();
    const { service } = await openChainOf(chainWithFiveMissing(), () => answer.promise);

    fireEvent.click(button());
    await waitFor(() => expect(button().disabled).toBe(true));
    fireEvent.click(button());
    fireEvent.click(button());

    expect(button().getAttribute('aria-busy')).toBe('true');
    expect(screen.getByText(/^Downloading\./)).toBeTruthy();
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

  it('reads as the answer of the download: every item as served, the lake reason and its own words for a failure, and the keyboard on it', async () => {
    await openChainOf(chainWithFiveMissing(), async () => answered());

    fireEvent.click(button());

    const result = await screen.findByRole('group', { name: 'Download result' });
    const lines = within(result).getAllByRole('listitem').map((line) => reads(line));
    expect.soft(lines).toEqual([
      'Listing SPY Already Stored',
      "Underlying's bars SPY Fetched",
      "Contract SPY Call 602 Oct 16 '26 Fetched",
      "Contract SPY Call 603 Oct 16 '26 Failed Provider No Data the vendor holds no trades for SPY261016C00603000 on 2026-10-09",
      "Contract SPY Call 606 Oct 16 '26 Failed Lease Timeout Being fetched by another request. Try again shortly. another request holds the claim",
    ]);
    expect.soft(reads(paragraph(/fetched, .* already stored/))).toBe('2 fetched, 1 already stored, 2 failed.');
    expect.soft(reads(await screen.findByRole('heading', { name: /Download of/ }))).toBe('Download of SPY on 2026-10-09');
    await waitFor(() => expect(document.activeElement).toBe(result));
  });

  it('is followed by reading the listing and the chain again, which now show what was fetched, and the answer stays', async () => {
    const day = vi.fn(async () => chainDay());
    const expiry = vi
      .fn()
      .mockResolvedValueOnce(chainWithFiveMissing())
      .mockResolvedValue(chainExpiry({ calls: side('call', [call(602_000, 'traded'), call(603_000, 'traded')]), puts: side('put', [put(601_000, 'traded')]) }));
    await renderArchive(fakeChainService(vi, { day, expiry, download: vi.fn(async () => answered()) }));
    await openStandardChain();

    fireEvent.click(button());

    await waitFor(() => expect(day).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(expiry).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Download missing data' })).toBeNull());
    expect(screen.getByRole('table', { name: /Calls on the left/ }).querySelectorAll('.call-side.last .vol-text, .put-side.last .vol-text')).toHaveLength(3);
    expect(screen.getByRole('group', { name: 'Download result' })).toBeTruthy();
  });

  it('keeps the listing and the chain on screen while they are read again, says so, and shows what was fetched when they arrive', async () => {
    const secondDay = deferred<ChainDay>();
    const secondChain = deferred<ChainExpiry>();
    const day = vi.fn().mockResolvedValueOnce(chainDay()).mockReturnValueOnce(secondDay.promise);
    const expiry = vi.fn().mockResolvedValueOnce(chainWithFiveMissing()).mockReturnValueOnce(secondChain.promise);
    await renderArchive(fakeChainService(vi, { day, expiry, download: vi.fn(async () => answered()) }));
    await openStandardChain();

    fireEvent.click(button());
    await waitFor(() => expect(day).toHaveBeenCalledTimes(2));

    expect(screen.getByRole('region', { name: 'Expiries listed that day' })).toBeTruthy();
    expect(screen.getByRole('table', { name: /Calls on the left/ })).toBeTruthy();
    expect(screen.queryByText('Reading the chain.')).toBeNull();
    expect(screen.getAllByText(/Reading (the listing )?again\. What is shown is the last answer\./)).toHaveLength(2);
    secondDay.resolve(chainDay());
    await waitFor(() => expect(expiry).toHaveBeenCalledTimes(2));
    secondChain.resolve(chainExpiry({ calls: side('call', [call(602_000, 'traded')]), puts: side('put', []) }));
    await waitFor(() => expect(screen.queryByText(/What is shown is the last answer\./)).toBeNull());

    expect(screen.getByRole('table', { name: /Calls on the left/ }).querySelectorAll('.call-side.last .vol-text')).toHaveLength(1);
  });

  it('is dropped when the owner picks another date, which it was not about', async () => {
    await openChainOf(chainWithFiveMissing(), async () => answered());
    fireEvent.click(button());
    await screen.findByRole('group', { name: 'Download result' });

    pickDate('2026-10-12');

    await waitFor(() => expect(screen.queryByRole('group', { name: 'Download result' })).toBeNull());
  });

  it('is dropped, not read as the answer for another instrument, when the place moves to one', async () => {
    const { host } = await openChainOf(chainWithFiveMissing(), async () => answered());
    fireEvent.click(button());
    await screen.findByRole('group', { name: 'Download result' });

    host.symbol.set('AAPL');

    await waitFor(() => expect(screen.queryByRole('group', { name: 'Download result' })).toBeNull());
  });
});

describe('a download the server will not take', () => {
  it('names the server cap in the server words when it refuses a call for naming too many, and leaves the button free', async () => {
    const refusal = new PastChainRefusedError('OPTIONS_BACKTEST_TOO_MANY_CONTRACTS', 'a download takes at most 60 contracts; 61 were named', 400);
    const { service } = await openChainOf(chainWithFiveMissing(), async () => {
      throw refusal;
    });

    fireEvent.click(button());

    expect((await screen.findAllByRole('alert')).map((alert) => reads(alert))).toContain('a download takes at most 60 contracts; 61 were named');
    expect(screen.getByText('Options Backtest Too Many Contracts')).toBeTruthy();
    expect(button().disabled).toBe(false);
    // Nothing was fetched, so nothing is read again.
    expect(service.day).toHaveBeenCalledTimes(1);
  });

  it.each([401, 403])('says this page is not allowed to start a download here when the data plane answers %i', async (status) => {
    await openChainOf(chainWithFiveMissing(), async () => {
      throw new DownloadNotAllowedError('The presented data-plane control secret does not match.', status);
    });

    fireEvent.click(button());

    expect(reads(await screen.findByRole('alert'))).toBe('This page is not allowed to start a download here.');
    expect(screen.getByText('The presented data-plane control secret does not match.')).toBeTruthy();
    expect(screen.queryByText(/could not be started/)).toBeNull();
  });

  it('says what the browser says when the call never came back, that it may or may not have happened, and reads the day and the chain again', async () => {
    const { service } = await openChainOf(chainWithFiveMissing(), async () => {
      throw new Error('network down');
    });

    fireEvent.click(button());

    expect(reads(await screen.findByRole('alert'))).toBe('The download could not be started.');
    expect(screen.getByText('The download may or may not have happened. The day has been read again: what is shown is what is stored.')).toBeTruthy();
    await waitFor(() => expect(service.day).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(service.expiry).toHaveBeenCalledTimes(2));
  });

  it.each([
    ['refuses the request', new PastChainRefusedError('OPTIONS_BACKTEST_TOO_MANY_CONTRACTS', 'too many', 400)],
    ['does not accept it (422)', new PastChainInvalidError([{ loc: ['body', 'contracts'], msg: 'Input should be a valid list' }])],
  ])('says nothing was downloaded, and reads nothing again, when the server %s', async (_name, refusal) => {
    const { service } = await openChainOf(chainWithFiveMissing(), async () => {
      throw refusal;
    });

    fireEvent.click(button());

    expect(await screen.findByText('Nothing was downloaded.')).toBeTruthy();
    await new Promise((done) => setTimeout(done, 20));
    expect(service.day).toHaveBeenCalledTimes(1);
  });
});

describe('a session the lake will fetch nothing for yet', () => {
  const REFUSAL: FetchRefusal = { reason: 'session_not_over', words: '2026-10-09 is not over in New York yet; its option data is taken from New York midnight after it.' };

  it('is the lake’s own words, its code in words under them, and nothing to press', async () => {
    const day = chainDay({ fetch_refusal: REFUSAL, listing_state: 'not_stored', expiries: [] });
    await renderArchive(fakeChainService(vi, { day: vi.fn(async () => day) }));

    pickDate(TRADING_DATE);

    expect(await screen.findByText(REFUSAL.words)).toBeTruthy();
    expect(screen.getByText('Session Not Over')).toBeTruthy();
    expect(screen.getByText('The listing for this date is not stored.')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Download missing data' })).toBeNull();
  });

  it('is said with the expiries when the listing is stored, and the contracts missing on screen are not offered either', async () => {
    const service = fakeChainService(vi, { day: vi.fn(async () => chainDay({ fetch_refusal: REFUSAL })), expiry: vi.fn(async () => chainWithFiveMissing()) });
    await renderArchive(service);
    await openStandardChain();

    expect(screen.getByText(REFUSAL.words)).toBeTruthy();
    expect(screen.getByRole('region', { name: 'Expiries listed that day' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Download missing data' })).toBeNull();
  });

  it('shows a code the page does not know through the shared label, with the lake’s words as it sent them', async () => {
    const unknown = { reason: 'quota_exceeded' as FetchRefusal['reason'], words: 'The vendor has no quota left today, so nothing is fetched until it resets.' };
    await renderArchive(fakeChainService(vi, { day: vi.fn(async () => chainDay({ fetch_refusal: unknown })) }));

    pickDate(TRADING_DATE);

    expect(await screen.findByText(unknown.words)).toBeTruthy();
    expect(screen.getByText('Quota Exceeded')).toBeTruthy();
  });

  it('is only said of that date: another date, whose day carries none, offers the download again', async () => {
    const day = vi.fn(async (query: { tradingDateMs: number }) => chainDay({ listing_state: 'not_stored', expiries: [], ...(query.tradingDateMs === TRADING_DATE_MS ? { fetch_refusal: REFUSAL } : {}) }));
    await renderArchive(fakeChainService(vi, { day }));
    pickDate(TRADING_DATE);
    await screen.findByText(REFUSAL.words);

    pickDate('2026-10-12');

    expect(await screen.findByRole('button', { name: 'Download missing data' })).toBeTruthy();
    expect(screen.queryByText(REFUSAL.words)).toBeNull();
  });
});

describe('a download of more contracts than one call takes', () => {
  const LIMITS: SetupLimits = { ...SERVED_LIMITS, max_contracts_per_download: 2 };
  // Five contracts are missing, in the server’s order: calls, then puts.
  const ORDER = ['SPY261016C00602000', 'SPY261016C00603000', 'SPY261016C00606000', 'SPY261016P00601000', 'SPY261016P00603000'];
  const state = (stored: ReadonlySet<string>, symbol: string) => (stored.has(symbol) ? 'traded' : 'not_stored');

  /** The chain of the five, with those a download has fetched now stored. */
  function chainAfter(stored: ReadonlySet<string>) {
    return chainExpiry({
      calls: side('call', [call(601_000, 'traded'), call(602_000, state(stored, ORDER[0])), call(603_000, state(stored, ORDER[1])), call(606_000, state(stored, ORDER[2]), false)]),
      puts: side('put', [put(601_000, state(stored, ORDER[3])), put(602_000, 'traded'), put(603_000, state(stored, ORDER[4]))]),
    });
  }

  async function openFive() {
    const stored = new Set<string>();
    const service = fakeChainService(vi, {
      expiry: vi.fn(async () => chainAfter(stored)),
      download: vi.fn(async (request: DownloadRequest) => {
        const named = request.contracts ?? [];
        named.forEach((symbol) => stored.add(symbol));
        return downloadAnswer(named.map((symbol) => item('contract', symbol, 'fetched')));
      }),
    });
    await renderArchive(service, LIMITS);
    await openStandardChain();
    return service;
  }
  const sentTo = (service: Awaited<ReturnType<typeof openFive>>, index: number) => service.download.mock.calls[index]?.[0]?.contracts;

  it('sends one call for one press, with the first it takes, in the server’s order, and the next press the next ones', async () => {
    const service = await openFive();

    fireEvent.click(button());
    await waitFor(() => expect(service.download).toHaveBeenCalledTimes(1));
    expect(sentTo(service, 0)).toEqual([ORDER[0], ORDER[1]]);
    await waitFor(() => expect(words()).toBe('Contracts to fetch in this call: 2. Still missing after it: 1. Press again for them. The most contracts one call takes: 2.'));
    fireEvent.click(button());

    await waitFor(() => expect(service.download).toHaveBeenCalledTimes(2));
    expect(sentTo(service, 1)).toEqual([ORDER[2], ORDER[3]]);
    await waitFor(() => expect(words()).toBe('Contracts to fetch: 1. The most contracts one call takes: 2.'));
    fireEvent.click(button());
    await waitFor(() => expect(service.download).toHaveBeenCalledTimes(3));
    expect(sentTo(service, 2)).toEqual([ORDER[4]]);
  });

  it('never asks for the rest by itself: no other call is made until the button is pressed', async () => {
    const service = await openFive();

    fireEvent.click(button());
    await waitFor(() => expect(service.download).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(words()).toContain('Still missing after it: 1'));
    await new Promise((done) => setTimeout(done, 80));

    expect(service.download).toHaveBeenCalledTimes(1);
  });
});
