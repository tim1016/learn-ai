import { screen, waitFor, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { deferred } from '../../../../components/options-backtest/testing/deferred';
import { paragraph, reads } from '../../../../components/options-backtest/testing/page-actions';
import { PastChainRefusedError } from './past-chain.service';
import type { ChainDay, ChainExpiry } from './past-chain.types';
import {
  chainDay,
  chainExpiry,
  contract,
  EXPIRY_MS,
  HALF_DAY,
  HALF_DAY_CLOSE_MS,
  MINUTE_1545_MS,
  side,
  summary,
  tag,
  TRADING_DATE,
  TRADING_DATE_MS,
} from './testing/fixtures';
import {
  callOf,
  chainTable,
  fakeChainService,
  openChainCard,
  openStandardChain,
  pickDate,
  putOf,
  renderArchive,
  ribbonButton,
  rowOf,
  showChain,
  typeTime,
} from './testing/render-archive';

const listing = () => screen.findByRole('region', { name: 'Expiries listed that day' });

describe('Past · Archive, before a date is picked', () => {
  it('asks for a date and reads nothing', async () => {
    const service = fakeChainService(vi);

    await renderArchive(service);

    expect(screen.getByText('No session date is picked.')).toBeTruthy();
    expect(service.day).not.toHaveBeenCalled();
    expect(service.expiry).not.toHaveBeenCalled();
    expect(screen.getByText(/Until you type a time, the chain is read at the close of the session/)).toBeTruthy();
  });

  it('reads a date the address names at once, for the instrument and the New York midnight of the date', async () => {
    const service = fakeChainService(vi);

    await renderArchive(service, undefined, { date: TRADING_DATE, symbol: 'aapl' });

    await listing();
    expect(service.day).toHaveBeenCalledTimes(1);
    expect(service.day.mock.calls[0]?.[0]).toEqual({ underlying: 'AAPL', tradingDateMs: TRADING_DATE_MS });
    expect((screen.getByLabelText('Trading date') as HTMLInputElement).value).toBe(TRADING_DATE);
  });
});

describe('the listing of a picked date', () => {
  it('puts every expiry in the ribbon in the order served, and opens none of them', async () => {
    const service = fakeChainService(vi);
    await renderArchive(service);

    pickDate(TRADING_DATE);

    const region = await listing();
    const days = within(region).getAllByRole('button').map((button) => reads(button));
    expect(days).toEqual(['9', '16', '30']);
    expect(within(region).getByText(/The listing held 3 expiries that day\. None is opened for you\./)).toBeTruthy();
    expect(service.expiry).not.toHaveBeenCalled();
  });

  it('says of each expiry, as the listing served it, its series, its distance, whether it expires today, and its counts', async () => {
    const day = chainDay({
      expiries: [
        summary(EXPIRY_MS['2026-10-09'], tag('monthly', { expires_today: true, days_to_expiry: 0 }), { calls: 120, standard_calls: 118, puts: 119, standard_puts: 117 }),
        summary(EXPIRY_MS['2026-10-30'], tag('unknown', { evidence: 'before tagging', days_to_expiry: 21 })),
      ],
    });
    await renderArchive(fakeChainService(vi, { day: vi.fn(async () => day) }));

    pickDate(TRADING_DATE);
    await listing();

    expect(ribbonButton('2026-10-09').getAttribute('title')).toBe('Monthly, expires today. 0 days to expiry. Calls: 120 listed, 118 standard. Puts: 119 listed, 117 standard.');
    expect(ribbonButton('2026-10-30').getAttribute('title')).toMatch(/^Unknown\. 21 days to expiry\./);
  });

  it('counts what the listing left out of its counts', async () => {
    const day = chainDay({ rows_left_out_unusable: 7, rows_left_out_expired: 3 });
    await renderArchive(fakeChainService(vi, { day: vi.fn(async () => day) }));

    pickDate(TRADING_DATE);

    expect(await screen.findByText(/7 rows name no contract a backtest can use/)).toBeTruthy();
    expect(screen.getByText(/3 are contracts that had already expired/)).toBeTruthy();
  });
});

describe('a day that has no expiries to pick from', () => {
  it('says the date is not a trading day in the server words, and offers no download', async () => {
    const service = fakeChainService(vi, {
      day: vi.fn(async () => {
        throw new PastChainRefusedError('OPTIONS_BACKTEST_NOT_A_SESSION', '2026-10-10 is a weekend day: the exchange had no session that day', 400);
      }),
    });
    await renderArchive(service);

    pickDate('2026-10-10');

    expect(await screen.findByText('Not a trading day.')).toBeTruthy();
    expect(screen.getByText(/2026-10-10 is a weekend day/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Download missing data' })).toBeNull();
  });

  it('says the listing is not stored, and offers the download', async () => {
    await renderArchive(fakeChainService(vi, { day: vi.fn(async () => chainDay({ listing_state: 'not_stored', expiries: [] })) }));

    pickDate(TRADING_DATE);

    expect(await screen.findByText('The listing for this date is not stored.')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Download missing data' })).toBeTruthy();
  });

  it('says the listing is stored and holds nothing a backtest can use, with the rows it left out', async () => {
    await renderArchive(fakeChainService(vi, { day: vi.fn(async () => chainDay({ expiries: [], rows_left_out_unusable: 12 })) }));

    pickDate(TRADING_DATE);

    expect(await screen.findByText('The listing for this date is stored, but it holds no contract a backtest can use.')).toBeTruthy();
    expect(screen.getByText(/12 rows name no contract a backtest can use/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Download missing data' })).toBeNull();
  });

  it('says the stored listing cannot be read, with the reason the server gave, and offers no download', async () => {
    const stored = chainDay({ listing_state: 'unreadable', listing_problem: 'the listing cannot be read: the stored file is damaged (OptionListingFileError)', expiries: [] });
    await renderArchive(fakeChainService(vi, { day: vi.fn(async () => stored) }));

    pickDate(TRADING_DATE);

    expect(await screen.findByText('The stored listing for this date cannot be read.')).toBeTruthy();
    expect(screen.getByText(/the stored file is damaged \(OptionListingFileError\)/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Download missing data' })).toBeNull();
  });

  it('says a listing state it does not know in words, and is neither a ribbon nor a listing that holds nothing', async () => {
    const odd = chainDay({ listing_state: 'partial' as ChainDay['listing_state'], expiries: [] });
    await renderArchive(fakeChainService(vi, { day: vi.fn(async () => odd) }));

    pickDate(TRADING_DATE);

    expect(await screen.findByText('This page does not know this state of the listing for this date: Partial.')).toBeTruthy();
    expect(screen.queryByRole('region', { name: 'Expiries listed that day' })).toBeNull();
    expect(screen.queryByText(/holds no contract a backtest can use/)).toBeNull();
  });

  it('says the catalog cannot be reached, with the server code, and tries again on request', async () => {
    const day = vi
      .fn()
      .mockRejectedValueOnce(new PastChainRefusedError('OPTIONS_BACKTEST_LAKE_CATALOG_UNAVAILABLE', "The data lake's catalog cannot be reached, so what is stored cannot be read.", 503))
      .mockResolvedValue(chainDay());
    await renderArchive(fakeChainService(vi, { day }));

    pickDate(TRADING_DATE);

    expect((await screen.findByRole('alert')).textContent).toContain("The data lake's catalog cannot be reached");
    expect(screen.getByText('Options Backtest Lake Catalog Unavailable')).toBeTruthy();
    screen.getByRole('button', { name: 'Try again' }).click();
    await listing();
    expect(day).toHaveBeenCalledTimes(2);
  });

  it('says a call that never reached the server could not be read, and offers another try', async () => {
    await renderArchive(fakeChainService(vi, { day: vi.fn().mockRejectedValue(new Error('network down')) }));

    pickDate(TRADING_DATE);

    expect((await screen.findByRole('alert')).textContent).toContain('The listing could not be read.');
    expect(screen.getByRole('button', { name: 'Try again' })).toBeTruthy();
  });
});

describe('the chain of one expiry', () => {
  it('is read once when an expiry is opened, with the expiry as the server gave it, and is not read before', async () => {
    const service = fakeChainService(vi);
    await renderArchive(service);
    pickDate(TRADING_DATE);
    await listing();
    expect(service.expiry).not.toHaveBeenCalled();
    expect(screen.getByText('No expiry is open.')).toBeTruthy();

    showChain('2026-10-16');
    await screen.findByRole('table', { name: /Calls on the left/ });

    expect(service.expiry).toHaveBeenCalledTimes(1);
    expect(service.expiry.mock.calls[0]?.[0]).toEqual({ underlying: 'SPY', tradingDateMs: TRADING_DATE_MS, expiryMs: EXPIRY_MS['2026-10-16'], atMs: null, strikes: null });
  });

  it('draws calls on the left, the money marked, and every contract by its priced trade with its time, or in words by why it has none', async () => {
    await renderArchive(fakeChainService(vi));

    await openStandardChain();

    expect.soft(within(chainTable()).getAllByRole('columnheader').map((header) => reads(header)).filter((text) => text !== ''), 'puts calls on the left, the strikes down the middle and puts on the right').toEqual(['Calls', 'Strike', 'Puts', 'Last', 'Strike', 'Last']);
    expect.soft([...chainTable().querySelectorAll('.strike-cell')].map((cell) => reads(cell).split(' ')[0]), 'puts the strikes in order').toEqual(['$601.00', '$602.00', '$603.00']);
    expect.soft(reads(rowOf('$602.00').querySelector('.strike-cell')), 'marks the money strike as the server served it').toContain('At the money');
    expect.soft(callOf(rowOf('$602.00')), 'says the price and the time of the trade, in New York time: the 602 call').toMatch(/\$2\.10 in the minute ending 15:59 ET/);
    expect.soft(callOf(rowOf('$601.00')), 'says the price and the time of the trade, in New York time: the 601 call').toMatch(/\$3\.25 in the minute ending 15:58 ET/);
    expect.soft(putOf(rowOf('$603.00')), 'says the price and the time of the trade, in New York time: the 603 put').toMatch(/\$1\.05 in the minute ending 15:45 ET/);
    expect.soft(callOf(rowOf('$603.00')), 'says in words that a contract did not trade').toContain('Did not trade by then');
    expect.soft(putOf(rowOf('$601.00')), 'says in words that a contract has no prices stored').toContain('Listed; prices not stored');
    expect.soft(putOf(rowOf('$602.00')), 'says in words that a contract cannot be read, and why').toContain('Prices cannot be read: the bars of SPY261016P00602000 for 2026-10-09 cannot be read');
    expect.soft(chainTable().textContent, 'never shows a zero for a missing price').not.toMatch(/\$0\.00/);
    for (const cell of chainTable().querySelectorAll('td')) expect.soft(reads(cell), 'never shows a blank cell').not.toBe('');
  });

  it('names the underlying, the session, the instant, the expiry with its series and the standard counts, in New York terms', async () => {
    const odd = chainExpiry({ tag: { series: 'monthly', version: 1, evidence: 'the third Friday of its month', expires_today: true, days_to_expiry: 0 } });
    await renderArchive(fakeChainService(vi, { expiry: vi.fn(async () => odd) }));

    await openStandardChain();

    const facts = reads(document.querySelector('app-chain-facts'));
    expect(facts).toContain('Past · Archive');
    expect(facts).toContain('Session 2026-10-09');
    expect(facts).toContain('Priced as of 16:00 ET');
    expect(facts).toContain('Expiry 2026-10-16 Monthly Expires today');
    expect(facts).toContain('Calls: 3 listed, 3 standard. Puts: 3 listed, 3 standard.');
    expect(facts).toContain('SPY then $602.40 in the minute ending 16:00 ET');
    for (const stamp of document.querySelectorAll('app-chain-facts app-timestamp-display')) {
      expect.soft(['date-et', 'et']).toContain(stamp.getAttribute('data-timestamp-mode'));
    }
  });

  it('draws no price for a trade the server stamped after the instant it was asked at, and says why', async () => {
    const late = contract({ right: 'call', thousandths: 602_000, state: 'traded', price: '9.99', timeMs: 1_791_576_060_000 });
    const service = fakeChainService(vi, { expiry: vi.fn(async () => chainExpiry({ calls: side('call', [late]), puts: side('put', []) })) });
    await renderArchive(service);

    await openStandardChain();

    expect(callOf(rowOf('$602.00'))).toMatch(/^Observed after this instant, so not shown/);
    expect(chainTable().textContent).not.toContain('9.99');
  });

  it('says an expiry the address names but the date does not list is not listed, keeps the ribbon on screen, and opens no other', async () => {
    const service = fakeChainService(vi, { expiry: vi.fn(async () => chainExpiry({ expiry_listed: false, tag: null, calls: side('call', [], null), puts: side('put', [], null) })) });
    await renderArchive(service);
    pickDate(TRADING_DATE);
    await listing();

    showChain('2026-10-16');

    expect(await screen.findByText('The 2026-10-16 expiry is not listed on this date.')).toBeTruthy();
    expect(screen.getByRole('region', { name: 'Expiries listed that day' })).toBeTruthy();
    expect(screen.queryByRole('table', { name: /Calls on the left/ })).toBeNull();
    expect(service.expiry).toHaveBeenCalledTimes(1);
  });

  it('says a chain that cannot be had in the server words, with its code, and tries again on request', async () => {
    const expiry = vi
      .fn()
      .mockRejectedValueOnce(new PastChainRefusedError('OPTIONS_BACKTEST_LAKE_CATALOG_UNAVAILABLE', "The data lake's catalog cannot be reached, so what is stored cannot be read.", 503))
      .mockResolvedValue(chainExpiry());
    await renderArchive(fakeChainService(vi, { expiry }));
    pickDate(TRADING_DATE);
    await listing();
    showChain('2026-10-16');

    expect((await screen.findAllByRole('alert')).some((alert) => alert.textContent?.includes("The data lake's catalog cannot be reached"))).toBe(true);
    screen.getByRole('button', { name: 'Try again' }).click();

    expect(await screen.findByRole('table', { name: /Calls on the left/ })).toBeTruthy();
    expect(expiry).toHaveBeenCalledTimes(2);
  });

  it('says why no strikes are drawn when the underlying has no price, and not that nothing is listed', async () => {
    const odd = chainExpiry({ underlying_price: { state: 'no_bar_yet', price: null, price_time_ms: null, problem: null }, calls: side('call', [], null, { listed: 120, standard_listed: 118 }), puts: side('put', [], null, { listed: 118, standard_listed: 116 }) });
    await renderArchive(fakeChainService(vi, { expiry: vi.fn(async () => odd) }));

    await openChainCard();

    expect(await screen.findByText('No strikes are drawn.')).toBeTruthy();
    expect(screen.getByText(/No bar of the underlying had closed by this time/)).toBeTruthy();
    expect(screen.queryByRole('table', { name: /Calls on the left/ })).toBeNull();
    expect(screen.queryByText(/No contract is listed at this expiry/)).toBeNull();
  });
});

describe('the session date and the expiry', () => {
  it('are separate: a new date leaves the open expiry as it was, and it is read again on the new date', async () => {
    const service = fakeChainService(vi);
    const { host } = await renderArchive(service);
    await openStandardChain();

    pickDate('2026-10-12');
    await waitFor(() => expect(service.expiry).toHaveBeenCalledTimes(2));

    // New York midnight of 2026-10-12 is 04:00 UTC (daylight time).
    expect(service.expiry.mock.calls[1]?.[0]).toMatchObject({ tradingDateMs: 1_791_777_600_000, expiryMs: EXPIRY_MS['2026-10-16'] });
    expect(host.expiryMs()).toBe(EXPIRY_MS['2026-10-16']);
    expect(ribbonButton('2026-10-16').getAttribute('class')).toContain('bg-blue-600');
  });

  it('are separate: opening an expiry leaves the date as it was', async () => {
    const { host } = await renderArchive(fakeChainService(vi));
    pickDate(TRADING_DATE);
    await listing();

    showChain('2026-10-30');
    await screen.findByRole('table', { name: /Calls on the left/ });

    expect(host.date()).toBe(TRADING_DATE);
    expect((screen.getByLabelText('Trading date') as HTMLInputElement).value).toBe(TRADING_DATE);
  });

  it('keeps the expiry open when the new date does not list it, says so, and lists that date\'s expiries without opening another', async () => {
    const day = vi
      .fn()
      .mockResolvedValueOnce(chainDay())
      .mockResolvedValue(chainDay({ expiries: [summary(EXPIRY_MS['2026-10-30'], tag('weekly'))] }));
    const expiry = vi
      .fn()
      .mockResolvedValueOnce(chainExpiry())
      .mockResolvedValue(chainExpiry({ expiry_listed: false, tag: null, calls: side('call', [], null), puts: side('put', [], null) }));
    await renderArchive(fakeChainService(vi, { day, expiry }));
    await openStandardChain();

    pickDate('2026-10-12');

    expect(await screen.findByText('The 2026-10-16 expiry is not listed on this date.')).toBeTruthy();
    expect(within(await listing()).getAllByRole('button').map((button) => reads(button))).toEqual(['30']);
    expect(expiry).toHaveBeenLastCalledWith(expect.objectContaining({ expiryMs: EXPIRY_MS['2026-10-16'] }));
  });
});

describe('the time of day', () => {
  it('is the close of the session when nothing is typed, and says which time that is once the date is read', async () => {
    const service = fakeChainService(vi);
    await renderArchive(service);
    await openStandardChain();

    expect(reads(paragraph(/^Until you type a time/))).toBe('Until you type a time, the chain is read at the close, 16:00 ET');
    expect(service.expiry.mock.calls[0]?.[0]).toMatchObject({ atMs: null });
  });

  it('is read at the minute typed, as that instant on the New York day, and the chain is read again for it', async () => {
    const service = fakeChainService(vi);
    const { host } = await renderArchive(service);
    await openStandardChain();

    typeTime('15:45');

    await waitFor(() => expect(service.expiry).toHaveBeenCalledTimes(2));
    expect(host.atMs()).toBe(MINUTE_1545_MS);
    expect(service.expiry.mock.calls[1]?.[0]).toMatchObject({ atMs: MINUTE_1545_MS });
    expect(await screen.findByText(/Read at this time, with no later observation/)).toBeTruthy();
  });

  it('is refused at the box before anything is asked when it is outside the session, in words with the session of that day', async () => {
    const service = fakeChainService(vi);
    const { host } = await renderArchive(service);
    await openStandardChain();

    typeTime('09:29');

    expect((await screen.findAllByRole('alert')).map((alert) => reads(alert))).toContain('That is before the open. Enter a time from 09:30 ET to 16:00 ET.');
    expect(host.atMs()).toBeNull();
    expect(service.expiry).toHaveBeenCalledTimes(1);
  });

  it('is held to the early close of a half day, which the day answer states', async () => {
    const half = chainDay({ session_open_ms: 1_795_789_800_000, session_close_ms: HALF_DAY_CLOSE_MS });
    const service = fakeChainService(vi, { day: vi.fn(async () => half) });
    await renderArchive(service);
    pickDate(HALF_DAY);
    await listing();

    typeTime('13:01');

    expect((await screen.findAllByRole('alert')).map((alert) => reads(alert))).toContain('That is after the close. Enter a time from 09:30 ET to 13:00 ET.');
  });

  it('is carried onto a new date: the same clock time is read on it', async () => {
    const service = fakeChainService(vi);
    const { host } = await renderArchive(service);
    await openStandardChain();
    typeTime('15:45');
    await waitFor(() => expect(host.atMs()).toBe(MINUTE_1545_MS));

    pickDate('2026-10-12');

    // 15:45 ET on 2026-10-12 is 19:45 UTC, 04:00 UTC midnight plus 15 h 45 min.
    await waitFor(() => expect(host.atMs()).toBe(1_791_777_600_000 + 945 * 60_000));
    expect((screen.getByLabelText('Time of day (New York time)') as HTMLInputElement).value).toBe('15:45');
  });

  it('is the close again when the box is emptied, and half a time is not read', async () => {
    const service = fakeChainService(vi);
    const { host } = await renderArchive(service);
    await openStandardChain();
    typeTime('15:45');
    await waitFor(() => expect(host.atMs()).toBe(MINUTE_1545_MS));

    typeTime('');

    await waitFor(() => expect(host.atMs()).toBeNull());
  });

  it('is shown at the box in the server words when the server refuses it although the page let it through', async () => {
    const expiry = vi.fn().mockResolvedValueOnce(chainExpiry()).mockRejectedValue(new PastChainRefusedError('OPTIONS_BACKTEST_INSTANT_NOT_ON_A_MINUTE', 'The instant is not on a whole minute.', 400));
    await renderArchive(fakeChainService(vi, { expiry }));
    await openStandardChain();

    typeTime('15:45');

    expect((await screen.findAllByRole('alert')).map((alert) => reads(alert))).toContain('The instant is not on a whole minute.');
  });
});

describe('the instrument', () => {
  it('is the symbol it is given: a read asks for it in capitals, and a different one reads its listing again', async () => {
    const service = fakeChainService(vi);
    const { host } = await renderArchive(service, undefined, { symbol: 'spy', date: TRADING_DATE });
    await listing();

    host.symbol.set('qqq');

    await waitFor(() => expect(service.day).toHaveBeenCalledTimes(2));
    expect(service.day.mock.calls.map((call) => call[0].underlying)).toEqual(['SPY', 'QQQ']);
  });
});

describe('an answer that arrives after something else was asked for', () => {
  it('is not the listing on screen: the listing of the earlier date, arriving last, is dropped', async () => {
    const first = deferred<ChainDay>();
    const day = vi.fn().mockReturnValueOnce(first.promise).mockResolvedValueOnce(chainDay({ expiries: [summary(EXPIRY_MS['2026-10-30'], tag('weekly'))] }));
    await renderArchive(fakeChainService(vi, { day }));

    pickDate(TRADING_DATE);
    pickDate('2026-10-12');
    expect(within(await listing()).getAllByRole('button').map((button) => reads(button))).toEqual(['30']);
    first.resolve(chainDay());

    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(within(screen.getByRole('region', { name: 'Expiries listed that day' })).getAllByRole('button').map((button) => reads(button))).toEqual(['30']);
  });

  it('is not the chain on screen: the chain of the earlier expiry, arriving last, is dropped', async () => {
    const first = deferred<ChainExpiry>();
    const second = chainExpiry({ expiry_ms: EXPIRY_MS['2026-10-30'], calls: side('call', [contract({ right: 'call', thousandths: 650_000, state: 'did_not_trade' })], 650_000), puts: side('put', []) });
    const expiry = vi.fn().mockReturnValueOnce(first.promise).mockResolvedValueOnce(second);
    await renderArchive(fakeChainService(vi, { expiry }));
    pickDate(TRADING_DATE);
    await listing();

    showChain('2026-10-16');
    showChain('2026-10-30');
    await screen.findByRole('table', { name: /Calls on the left/ });
    first.resolve(chainExpiry());

    await new Promise((resolve) => setTimeout(resolve, 0));
    expect([...chainTable().querySelectorAll('.strike-cell')].map((cell) => reads(cell).split(' ')[0])).toEqual(['$650.00']);
  });
});
