import { describe, expect, it } from 'vitest';

import { NOT_A_SESSION, type ReadProblem } from './past-chain-problems';
import { cellOf, chainViewOf, dayViewOf, downloadPlan, downloadRequest, outcomeCounts, planWords, readStatusOf, sessionNotOver, sessionOf, strikeRows, underlyingViewOf, type ChainInputs } from './past-chain-view';
import { chainDay, chainExpiry, contract, downloadAnswer, EXPIRY_MS, item, side, SESSION_CLOSE_MS, SESSION_OPEN_MS, TRADING_DATE_MS, underlyingPrice } from './testing/fixtures';

const problem = (changes: Partial<ReadProblem> = {}): ReadProblem => ({ message: 'the server said so', code: 'CODE', control: 'area', canRetry: false, ...changes });

describe('what a read of the day comes to', () => {
  it('is to pick a date while nothing is asked, and to wait while it is asked', () => {
    expect(dayViewOf('idle', null, null)).toEqual({ kind: 'pick' });
    expect(dayViewOf('loading', null, null)).toEqual({ kind: 'loading' });
    expect(dayViewOf('reloading', chainDay(), null)).toEqual({ kind: 'loading' });
  });

  it('is the listing not being stored, its being unreadable with the lake reason, and its holding nothing usable', () => {
    expect(dayViewOf('resolved', chainDay({ listing_state: 'not_stored', expiries: [] }), null)).toEqual({ kind: 'not_stored' });
    expect(dayViewOf('resolved', chainDay({ listing_state: 'unreadable', listing_problem: 'cannot be read (OptionListingFileError)', expiries: [] }), null)).toEqual({
      kind: 'unreadable',
      problem: 'cannot be read (OptionListingFileError)',
    });
    const empty = chainDay({ expiries: [], rows_left_out_unusable: 4 });
    expect(dayViewOf('resolved', empty, null)).toEqual({ kind: 'empty', day: empty });
  });

  it('is the expiries when the listing is stored and holds some', () => {
    const day = chainDay();

    expect(dayViewOf('resolved', day, null)).toEqual({ kind: 'listed', day });
  });

  it('is the server sentence when the date is not a trading day', () => {
    expect(dayViewOf('error', null, problem({ code: NOT_A_SESSION, message: '2026-10-10 is a weekend day' }))).toEqual({ kind: 'not_a_session', message: '2026-10-10 is a weekend day' });
  });

  it('is sent to the control when the server would not take the underlying or the date, and to the area otherwise', () => {
    expect(dayViewOf('error', null, problem({ control: 'underlying' }))).toEqual({ kind: 'at_control', control: 'underlying' });
    expect(dayViewOf('error', null, problem({ control: 'date' }))).toEqual({ kind: 'at_control', control: 'date' });
    expect(dayViewOf('error', null, problem({ canRetry: true }))).toEqual({ kind: 'failed', message: 'the server said so', code: 'CODE', canRetry: true });
  });

  it('never shows a listing it was not given: a read with no day and no reason is a failure in words', () => {
    expect(dayViewOf('error', null, null)).toEqual({ kind: 'failed', message: 'The listing could not be read.', code: null, canRetry: true });
    expect(dayViewOf('resolved', null, null)).toMatchObject({ kind: 'failed' });
  });

  it('counts a reload as asking, and local, resolved as answered', () => {
    expect(readStatusOf('reloading')).toBe('loading');
    expect(readStatusOf('local')).toBe('ready');
    expect(readStatusOf('resolved')).toBe('ready');
  });

  it('has the session of the day from the answer', () => {
    expect(sessionOf(chainDay())).toEqual({ openMs: SESSION_OPEN_MS, closeMs: SESSION_CLOSE_MS });
  });
});

describe('what the read of one expiry comes to', () => {
  const ready: ChainInputs = { dayReady: true, expiryPicked: true, blockedBy: null, status: 'resolved', chain: chainExpiry(), problem: null };

  it('says nothing of its own while the day cannot be read, and asks for an expiry before one is picked', () => {
    expect(chainViewOf({ ...ready, dayReady: false })).toEqual({ kind: 'no_day' });
    expect(chainViewOf({ ...ready, expiryPicked: false })).toEqual({ kind: 'no_expiry' });
  });

  it('is blocked by a control that holds something it cannot be asked with, ahead of any read', () => {
    expect(chainViewOf({ ...ready, blockedBy: 'time', status: 'idle', chain: null })).toEqual({ kind: 'blocked', control: 'time' });
    expect(chainViewOf({ ...ready, blockedBy: 'strikes' })).toEqual({ kind: 'blocked', control: 'strikes' });
  });

  it('waits while it is asked, including before the read has started', () => {
    expect(chainViewOf({ ...ready, status: 'loading', chain: null })).toEqual({ kind: 'loading' });
    expect(chainViewOf({ ...ready, status: 'idle', chain: null })).toEqual({ kind: 'loading' });
  });

  it('shows the chain when the expiry is listed', () => {
    expect(chainViewOf(ready)).toEqual({ kind: 'shown', chain: chainExpiry() });
  });

  it('says the expiry is not listed on the date, and not that it is unknown, when the server says false', () => {
    expect(chainViewOf({ ...ready, chain: chainExpiry({ expiry_listed: false, tag: null }) })).toEqual({ kind: 'not_listed' });
  });

  it('says whether it is listed cannot be told when the listing is not stored or cannot be read', () => {
    expect(chainViewOf({ ...ready, chain: chainExpiry({ expiry_listed: null, tag: null, listing_state: 'not_stored' }) })).toEqual({ kind: 'listing_unknown', state: 'not_stored', problem: null });
    expect(chainViewOf({ ...ready, chain: chainExpiry({ expiry_listed: null, tag: null, listing_state: 'unreadable', listing_problem: 'damaged' }) })).toEqual({
      kind: 'listing_unknown',
      state: 'unreadable',
      problem: 'damaged',
    });
  });

  it('puts a refusal about a control at that control and any other in the area, with whether it can be tried again', () => {
    expect(chainViewOf({ ...ready, status: 'error', chain: null, problem: problem({ control: 'time' }) })).toEqual({ kind: 'blocked', control: 'time' });
    expect(chainViewOf({ ...ready, status: 'error', chain: null, problem: problem({ control: 'strikes' }) })).toEqual({ kind: 'blocked', control: 'strikes' });
    expect(chainViewOf({ ...ready, status: 'error', chain: null, problem: problem({ control: 'underlying' }) })).toEqual({ kind: 'blocked', control: 'underlying' });
    expect(chainViewOf({ ...ready, status: 'error', chain: null, problem: problem({ canRetry: true }) })).toEqual({ kind: 'failed', message: 'the server said so', code: 'CODE', canRetry: true });
    expect(chainViewOf({ ...ready, status: 'error', chain: null, problem: null })).toMatchObject({ kind: 'failed', message: 'The chain could not be read.' });
  });
});

describe('the strikes of the table', () => {
  const call = (thousandths: number) => contract({ right: 'call', thousandths, state: 'did_not_trade' });
  const put = (thousandths: number) => contract({ right: 'put', thousandths, state: 'did_not_trade' });

  it('has one row for a strike both sides list, calls and puts side by side', () => {
    const rows = strikeRows(side('call', [call(601_000), call(602_000)]), side('put', [put(601_000), put(602_000)]));

    expect(rows.map((row) => [row.thousandths, row.call?.symbol, row.put?.symbol])).toEqual([
      [601_000, 'SPY261016C00601000', 'SPY261016P00601000'],
      [602_000, 'SPY261016C00602000', 'SPY261016P00602000'],
    ]);
  });

  it('keeps a strike one side lists as a row with the other side empty, in strike order, and not a row of nothing', () => {
    const rows = strikeRows(side('call', [call(600_000), call(602_000), call(604_000)]), side('put', [put(601_000), put(602_000), put(603_000)]));

    expect(rows.map((row) => [row.thousandths, row.call !== null, row.put !== null])).toEqual([
      [600_000, true, false],
      [601_000, false, true],
      [602_000, true, true],
      [603_000, false, true],
      [604_000, true, false],
    ]);
  });

  it('handles a side with nothing listed, and both', () => {
    expect(strikeRows(side('call', []), side('put', [put(1_000)])).map((row) => [row.strike, row.call, row.put !== null])).toEqual([['1', null, true]]);
    expect(strikeRows(side('call', [call(2_500)]), side('put', [])).map((row) => [row.strike, row.call !== null, row.put])).toEqual([['2.5', true, null]]);
    expect(strikeRows(side('call', [], null), side('put', [], null))).toEqual([]);
  });

  it('writes the strike as the server wrote it', () => {
    const rows = strikeRows(side('call', [call(602_500)]), side('put', [put(602_500)]));

    expect(rows[0]?.strike).toBe('602.5');
  });

  it('marks the strike the calls own money is at, and the strike the puts own money is at, apart', () => {
    const rows = strikeRows(side('call', [call(601_000), call(602_000), call(603_000)], 602_000), side('put', [put(601_000), put(602_000), put(603_000)], 603_000));

    expect(rows.map((row) => [row.thousandths, row.callMoney, row.putMoney])).toEqual([
      [601_000, false, false],
      [602_000, true, false],
      [603_000, false, true],
    ]);
  });

  it('marks a strike both sides have their money at as the money of both', () => {
    const rows = strikeRows(side('call', [call(602_000)], 602_000), side('put', [put(602_000)], 602_000));

    expect(rows[0]).toMatchObject({ callMoney: true, putMoney: true });
  });

  it('marks nothing when a side has no money strike, and a money strike on a side that does not list it marks that side only where it lists it', () => {
    const none = strikeRows(side('call', [call(602_000)], null), side('put', [put(602_000)], null));
    expect(none[0]).toMatchObject({ callMoney: false, putMoney: false });

    // The puts' money is 601, a strike only the calls list: no put sits on it, so no put is marked.
    const calls = side('call', [call(601_000), call(602_000)], 602_000);
    const puts = side('put', [put(602_000)], 601_000);
    expect(strikeRows(calls, puts).map((row) => [row.thousandths, row.callMoney, row.putMoney])).toEqual([
      [601_000, false, false],
      [602_000, true, false],
    ]);
  });
});

describe('what one contract cell says', () => {
  it('is a price with the time of the trade it is the price of', () => {
    expect(cellOf(contract({ right: 'call', thousandths: 602_000, state: 'traded', price: '2.10', timeMs: 1_791_575_940_000 }))).toEqual({ kind: 'traded', price: '2.10', timeMs: 1_791_575_940_000 });
  });

  it('is never a price when the server left out the time of the trade, or the price of a trade', () => {
    const traded = contract({ right: 'call', thousandths: 602_000, state: 'traded' });

    expect(cellOf({ ...traded, price_time_ms: null })).toEqual({ kind: 'incomplete', missing: 'time' });
    expect(cellOf({ ...traded, price: null })).toEqual({ kind: 'incomplete', missing: 'price' });
  });

  it('is not a price for a contract that did not trade, that has no prices stored, or whose prices cannot be read, even if a price came with it', () => {
    const stray = { price: '9.99', price_time_ms: 1_791_575_940_000 };

    expect(cellOf({ ...contract({ right: 'call', thousandths: 1, state: 'did_not_trade' }), ...stray })).toEqual({ kind: 'did_not_trade' });
    expect(cellOf({ ...contract({ right: 'call', thousandths: 1, state: 'not_stored' }), ...stray })).toEqual({ kind: 'not_stored' });
    expect(cellOf({ ...contract({ right: 'call', thousandths: 1, state: 'unreadable', problem: 'damaged' }), ...stray })).toEqual({ kind: 'unreadable', problem: 'damaged' });
  });

  it('is the same for the underlying: a price with its time, or the reason it has none', () => {
    expect(underlyingViewOf(underlyingPrice())).toEqual({ kind: 'priced', price: '602.40', timeMs: SESSION_CLOSE_MS });
    expect(underlyingViewOf(underlyingPrice({ price_time_ms: null }))).toEqual({ kind: 'incomplete', missing: 'time' });
    expect(underlyingViewOf(underlyingPrice({ price: null }))).toEqual({ kind: 'incomplete', missing: 'price' });
    expect(underlyingViewOf(underlyingPrice({ state: 'no_bar_yet', price: null, price_time_ms: null }))).toEqual({ kind: 'no_bar_yet' });
    expect(underlyingViewOf(underlyingPrice({ state: 'not_stored', price: null, price_time_ms: null }))).toEqual({ kind: 'not_stored' });
    expect(underlyingViewOf(underlyingPrice({ state: 'unreadable', price: null, price_time_ms: null, problem: 'damaged' }))).toEqual({ kind: 'unreadable', problem: 'damaged' });
  });
});

describe('what a download would fetch', () => {
  const stored = (right: 'call' | 'put', thousandths: number) => contract({ right, thousandths, state: 'traded' });
  const missing = (right: 'call' | 'put', thousandths: number) => contract({ right, thousandths, state: 'not_stored' });

  const allStored = () => chainExpiry({ calls: side('call', [stored('call', 602_000), contract({ right: 'call', thousandths: 603_000, state: 'did_not_trade' })]), puts: side('put', [stored('put', 602_000)]) });

  it('is nothing when everything on screen is stored', () => {
    expect(downloadPlan(chainDay(), allStored())).toBeNull();
    expect(downloadPlan(null, null)).toBeNull();
  });

  it('is the listing when it is not stored, and the underlying is then not known to be missing', () => {
    expect(downloadPlan(chainDay({ listing_state: 'not_stored', expiries: [] }), null)).toEqual({ listing: true, underlying: 'unknown', contracts: [] });
  });

  it('is not offered for a listing that is stored but cannot be read, nor for contracts whose prices cannot be read', () => {
    expect(downloadPlan(chainDay({ listing_state: 'unreadable', expiries: [] }), null)).toBeNull();
    const unreadable = chainExpiry({ calls: side('call', [contract({ right: 'call', thousandths: 602_000, state: 'unreadable' })]), puts: side('put', []) });
    expect(downloadPlan(chainDay(), unreadable)).toBeNull();
  });

  it('is the underlying bars when the chain says they are not stored', () => {
    const chain = { ...allStored(), underlying_price: underlyingPrice({ state: 'not_stored', price: null, price_time_ms: null }) };

    expect(downloadPlan(chainDay(), chain)).toEqual({ listing: false, underlying: 'missing', contracts: [] });
  });

  it('is exactly the contracts on screen whose prices are not stored, calls then puts, by the symbols the server gave', () => {
    const chain = chainExpiry({
      calls: side('call', [stored('call', 601_000), missing('call', 602_000), missing('call', 603_000)]),
      puts: side('put', [missing('put', 601_000), stored('put', 602_000), missing('put', 604_000)]),
    });

    expect(downloadPlan(chainDay(), chain)).toEqual({
      listing: false,
      underlying: 'held',
      contracts: ['SPY261016C00602000', 'SPY261016C00603000', 'SPY261016P00601000', 'SPY261016P00604000'],
    });
  });

  it('counts a contract not standard like any other, and leaves out one that is only not read', () => {
    const odd = { ...missing('call', 650_000), standard: false, steps_from_the_money: null };
    const chain = chainExpiry({ calls: side('call', [odd]), puts: side('put', [contract({ right: 'put', thousandths: 650_000, state: 'did_not_trade' })]) });

    expect(downloadPlan(chainDay(), chain)?.contracts).toEqual(['SPY261016C00650000']);
  });

  it('says in words what it will fetch', () => {
    expect(planWords({ listing: true, underlying: 'missing', contracts: ['a', 'b', 'c'] })).toBe("the listing, the underlying's bars and 3 contracts");
    expect(planWords({ listing: false, underlying: 'held', contracts: ['a'] })).toBe('1 contract');
    expect(planWords({ listing: false, underlying: 'missing', contracts: ['a', 'b'] })).toBe("the underlying's bars and 2 contracts");
    expect(planWords({ listing: true, underlying: 'unknown', contracts: [] })).toBe("the listing and the underlying's bars if they are missing");
    expect(planWords({ listing: true, underlying: 'held', contracts: [] })).toBe('the listing');
    expect(planWords({ listing: false, underlying: 'held', contracts: [] })).toBe('nothing');
  });

  it('is a request of the underlying, the trading date, and the contracts, with the expiry only to check them against', () => {
    const plan = { listing: false, underlying: 'held' as const, contracts: ['SPY261016C00602000'] };

    expect(downloadRequest(plan, 'SPY', TRADING_DATE_MS, EXPIRY_MS['2026-10-16'])).toEqual({
      underlying: 'SPY',
      trading_date_ms: 1_791_518_400_000,
      expiry_ms: 1_792_180_800_000,
      contracts: ['SPY261016C00602000'],
    });
  });

  it('names no expiry when it names no contract', () => {
    const request = downloadRequest({ listing: true, underlying: 'unknown', contracts: [] }, 'SPY', TRADING_DATE_MS, EXPIRY_MS['2026-10-16']);

    expect(request).toEqual({ underlying: 'SPY', trading_date_ms: TRADING_DATE_MS, contracts: [] });
    expect('expiry_ms' in request).toBe(false);
  });
});

describe('what an answer of a download comes to', () => {
  it('says the session is not over when the lake refused any item for that reason', () => {
    expect(sessionNotOver(downloadAnswer([item('listing', 'SPY', 'failed', 'session_not_over', 'not over')]))).toBe(true);
    expect(sessionNotOver(downloadAnswer([item('listing', 'SPY', 'fetched'), item('underlying', 'SPY', 'failed', 'provider_rate_limited')]))).toBe(false);
  });

  it('counts the items by how each came out', () => {
    const answer = downloadAnswer([
      item('listing', 'SPY', 'fetched'),
      item('underlying', 'SPY', 'already_stored'),
      item('contract', 'SPY261016C00602000', 'fetched'),
      item('contract', 'SPY261016C00603000', 'failed', 'lease_timeout'),
      item('contract', 'SPY261016C00604000', 'failed', 'provider_no_data'),
    ]);

    expect(outcomeCounts(answer)).toEqual({ fetched: 2, alreadyStored: 1, failed: 2 });
  });
});
