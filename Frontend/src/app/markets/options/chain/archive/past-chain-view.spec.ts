import { describe, expect, it } from 'vitest';

import { NOT_A_SESSION, type ReadProblem } from './past-chain-problems';
import type { ChainContract, ChainDay, ChainExpiry, UnderlyingPrice } from './past-chain.types';
import { bodyOf, cellOf, chainViewOf, dayViewOf, downloadPlan, downloadRequest, outcomeCounts, planWords, readStatusOf, strikeRows, underlyingViewOf, type ChainInputs } from './past-chain-view';
import { chainDay, chainExpiry, contract, downloadAnswer, EXPIRY_MS, item, side, sideWithoutMoney, SESSION_CLOSE_MS, SESSION_OPEN_MS, TRADING_DATE_MS, underlyingPrice } from './testing/fixtures';

const problem = (changes: Partial<ReadProblem> = {}): ReadProblem => ({ message: 'the server said so', code: 'CODE', control: 'area', canRetry: false, ...changes });

describe('what a read of the day comes to', () => {
  it('comes to a view by the status of the read and by what the listing holds', () => {
    expect.soft(dayViewOf('idle', null, null), 'is to pick a date while nothing is asked, and to wait while it is asked (1 of 2)').toEqual({ kind: 'pick' });
    expect.soft(dayViewOf('loading', null, null), 'is to pick a date while nothing is asked, and to wait while it is asked (2 of 2)').toEqual({ kind: 'loading' });

    const day = chainDay();

    expect.soft(dayViewOf('loading', day, null), 'keeps showing the listing it was given while it is read again, and says it is loading only when it has none (1 of 2)').toEqual({ kind: 'listed', day });
    expect.soft(dayViewOf('reloading', day, null), 'keeps showing the listing it was given while it is read again, and says it is loading only when it has none (2 of 2)').toEqual({ kind: 'listed', day });

    expect.soft(dayViewOf('resolved', chainDay({ listing_state: 'not_stored', expiries: [] }), null), 'is the listing not being stored, its being unreadable with the lake reason, and its holding nothing usable (1 of 3)').toEqual({ kind: 'not_stored' });
    expect.soft(dayViewOf('resolved', chainDay({ listing_state: 'unreadable', listing_problem: 'cannot be read (OptionListingFileError)', expiries: [] }), null), 'is the listing not being stored, its being unreadable with the lake reason, and its holding nothing usable (2 of 3)').toEqual({
      kind: 'unreadable',
      problem: 'cannot be read (OptionListingFileError)',
    });
    const empty = chainDay({ expiries: [], rows_left_out_unusable: 4 });
    expect.soft(dayViewOf('resolved', empty, null), 'is the listing not being stored, its being unreadable with the lake reason, and its holding nothing usable (3 of 3)').toEqual({ kind: 'empty', day: empty });

    const odd = chainDay({ listing_state: 'partial' as ChainDay['listing_state'] });

    expect.soft(dayViewOf('resolved', odd, null), 'says in words the code of a listing state it does not know, and is neither a table nor a stored listing (1 of 2)').toEqual({ kind: 'unknown_listing', code: 'partial' });
    expect.soft(dayViewOf('resolved', { ...odd, expiries: [] }, null), 'says in words the code of a listing state it does not know, and is neither a table nor a stored listing (2 of 2)').toEqual({ kind: 'unknown_listing', code: 'partial' });

    const holding = chainDay();

    expect.soft(dayViewOf('resolved', holding, null), 'is the expiries when the listing is stored and holds some').toEqual({ kind: 'listed', day: holding });

    expect.soft(readStatusOf('reloading'), 'counts a reload as asking, and local, resolved as answered (1 of 3)').toBe('loading');
    expect.soft(readStatusOf('local'), 'counts a reload as asking, and local, resolved as answered (2 of 3)').toBe('ready');
    expect.soft(readStatusOf('resolved'), 'counts a reload as asking, and local, resolved as answered (3 of 3)').toBe('ready');
  });

  it('comes to a view by the problem the read ended in', () => {
    expect.soft(dayViewOf('error', null, problem({ code: NOT_A_SESSION, message: '2026-10-10 is a weekend day' })), 'is the server sentence when the date is not a trading day').toEqual({ kind: 'not_a_session', message: '2026-10-10 is a weekend day' });

    expect.soft(dayViewOf('error', null, problem({ control: 'underlying' })), 'is sent to the control when the server would not take the underlying or the date, and to the area otherwise (1 of 3)').toEqual({ kind: 'at_control', control: 'underlying' });
    expect.soft(dayViewOf('error', null, problem({ control: 'date' })), 'is sent to the control when the server would not take the underlying or the date, and to the area otherwise (2 of 3)').toEqual({ kind: 'at_control', control: 'date' });
    expect.soft(dayViewOf('error', null, problem({ canRetry: true })), 'is sent to the control when the server would not take the underlying or the date, and to the area otherwise (3 of 3)').toEqual({ kind: 'failed', message: 'the server said so', code: 'CODE', canRetry: true });

    expect.soft(dayViewOf('error', null, null), 'never shows a listing it was not given: a read with no day and no reason is a failure in words (1 of 2)').toEqual({ kind: 'failed', message: 'The listing could not be read.', code: null, canRetry: true });
    expect.soft(dayViewOf('resolved', null, null), 'never shows a listing it was not given: a read with no day and no reason is a failure in words (2 of 2)').toMatchObject({ kind: 'failed' });
  });
});

describe('what the read of one expiry comes to', () => {
  const ready: ChainInputs = { dayReady: true, expiryPicked: true, blockedBy: null, status: 'resolved', chain: chainExpiry(), problem: null };

  it('comes to a view by the state of the read and of the listing the expiry is read from', () => {
    expect.soft(chainViewOf({ ...ready, dayReady: false }), 'says nothing of its own while the day cannot be read, and asks for an expiry before one is picked (1 of 2)').toEqual({ kind: 'no_day' });
    expect.soft(chainViewOf({ ...ready, expiryPicked: false }), 'says nothing of its own while the day cannot be read, and asks for an expiry before one is picked (2 of 2)').toEqual({ kind: 'no_expiry' });

    expect.soft(chainViewOf({ ...ready, blockedBy: 'time', status: 'idle', chain: null }), 'is blocked by a control that holds something it cannot be asked with, ahead of any read (1 of 2)').toEqual({ kind: 'blocked', control: 'time' });
    expect.soft(chainViewOf({ ...ready, blockedBy: 'strikes' }), 'is blocked by a control that holds something it cannot be asked with, ahead of any read (2 of 2)').toEqual({ kind: 'blocked', control: 'strikes' });

    expect.soft(chainViewOf({ ...ready, status: 'loading', chain: null }), 'waits while it is asked, including before the read has started (1 of 2)').toEqual({ kind: 'loading' });
    expect.soft(chainViewOf({ ...ready, status: 'idle', chain: null }), 'waits while it is asked, including before the read has started (2 of 2)').toEqual({ kind: 'loading' });

    expect.soft(chainViewOf(ready), 'shows the chain when the expiry is listed').toEqual({ kind: 'shown', chain: chainExpiry() });

    expect.soft(chainViewOf({ ...ready, chain: chainExpiry({ expiry_listed: false, tag: null }) }), 'says the expiry is not listed on the date, and not that it is unknown, when the server says false').toEqual({ kind: 'not_listed' });

    expect.soft(chainViewOf({ ...ready, chain: chainExpiry({ expiry_listed: null, tag: null, listing_state: 'not_stored' }) }), 'says whether it is listed cannot be told when the listing is not stored or cannot be read (1 of 2)').toEqual({ kind: 'listing_unknown', state: 'not_stored', problem: null });
    expect.soft(chainViewOf({ ...ready, chain: chainExpiry({ expiry_listed: null, tag: null, listing_state: 'unreadable', listing_problem: 'damaged' }) }), 'says whether it is listed cannot be told when the listing is not stored or cannot be read (2 of 2)').toEqual({
      kind: 'listing_unknown',
      state: 'unreadable',
      problem: 'damaged',
    });

    expect.soft(chainViewOf({ ...ready, status: 'loading' }), 'shows the chain it was given while the same chain is read again, and waits only when it has none (1 of 2)').toEqual({ kind: 'shown', chain: chainExpiry() });
    expect.soft(chainViewOf({ ...ready, status: 'idle' }), 'shows the chain it was given while the same chain is read again, and waits only when it has none (2 of 2)').toEqual({ kind: 'shown', chain: chainExpiry() });

    const odd = 'partial' as ChainExpiry['listing_state'];

    expect.soft(chainViewOf({ ...ready, chain: chainExpiry({ listing_state: odd }) }), 'says in words the code of a listing state it does not know, whatever else the answer says of the expiry (1 of 3)').toEqual({ kind: 'unknown_listing', code: 'partial' });
    expect.soft(chainViewOf({ ...ready, chain: chainExpiry({ listing_state: odd, expiry_listed: null, tag: null }) }), 'says in words the code of a listing state it does not know, whatever else the answer says of the expiry (2 of 3)').toEqual({ kind: 'unknown_listing', code: 'partial' });
    expect.soft(chainViewOf({ ...ready, chain: chainExpiry({ listing_state: odd, expiry_listed: false, tag: null }) }), 'says in words the code of a listing state it does not know, whatever else the answer says of the expiry (3 of 3)').toEqual({ kind: 'unknown_listing', code: 'partial' });

    expect.soft(chainViewOf({ ...ready, chain: chainExpiry({ expiry_listed: null, tag: null, listing_state: 'stored' }) }), 'does not call a stored listing not stored when the server gave no answer on whether the expiry is in it').toEqual({
      kind: 'failed',
      message: 'The server did not say whether the expiry is listed.',
      code: null,
      canRetry: false,
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

  it('lays the strikes out as rows, side by side or with one side empty, in strike order, with the strike written as the server wrote it', () => {
    const rows = strikeRows(side('call', [call(601_000), call(602_000)]), side('put', [put(601_000), put(602_000)]));

    expect.soft(rows.map((row) => [row.thousandths, row.call?.symbol, row.put?.symbol]), 'has one row for a strike both sides list, calls and puts side by side').toEqual([
      [601_000, 'SPY261016C00601000', 'SPY261016P00601000'],
      [602_000, 'SPY261016C00602000', 'SPY261016P00602000'],
    ]);

    const ordered = strikeRows(side('call', [call(600_000), call(602_000), call(604_000)]), side('put', [put(601_000), put(602_000), put(603_000)]));

    expect.soft(ordered.map((row) => [row.thousandths, row.call !== null, row.put !== null]), 'keeps a strike one side lists as a row with the other side empty, in strike order, and not a row of nothing').toEqual([
      [600_000, true, false],
      [601_000, false, true],
      [602_000, true, true],
      [603_000, false, true],
      [604_000, true, false],
    ]);

    expect.soft(strikeRows(side('call', []), side('put', [put(1_000)])).map((row) => [row.strike, row.call, row.put !== null]), 'handles a side with nothing listed, and both (1 of 3)').toEqual([['1', null, true]]);
    expect.soft(strikeRows(side('call', [call(2_500)]), side('put', [])).map((row) => [row.strike, row.call !== null, row.put]), 'handles a side with nothing listed, and both (2 of 3)').toEqual([['2.5', true, null]]);
    expect.soft(strikeRows(side('call', [], null), side('put', [], null)), 'handles a side with nothing listed, and both (3 of 3)').toEqual([]);

    const written = strikeRows(side('call', [call(602_500)]), side('put', [put(602_500)]));

    expect.soft(written[0]?.strike, 'writes the strike as the server wrote it').toBe('602.5');
  });

  it('marks the money of each side at its own strike, of both at one strike, and of neither when a side has no money strike', () => {
    const rows = strikeRows(side('call', [call(601_000), call(602_000), call(603_000)], 602_000), side('put', [put(601_000), put(602_000), put(603_000)], 603_000));

    expect.soft(rows.map((row) => [row.thousandths, row.callMoney, row.putMoney]), 'marks the strike the calls own money is at, and the strike the puts own money is at, apart').toEqual([
      [601_000, false, false],
      [602_000, true, false],
      [603_000, false, true],
    ]);

    const both = strikeRows(side('call', [call(602_000)], 602_000), side('put', [put(602_000)], 602_000));

    expect.soft(both[0], 'marks a strike both sides have their money at as the money of both').toMatchObject({ callMoney: true, putMoney: true });

    const putsOnly = strikeRows(side('call', [call(601_000)], 601_000), side('put', [put(601_000), put(602_000)], 602_000));

    expect.soft(putsOnly.map((row) => [row.thousandths, row.callMoney, row.putMoney]), 'marks the money of the puts at a strike only the puts list, and not the calls').toEqual([
      [601_000, true, false],
      [602_000, false, true],
    ]);

    const none = strikeRows(side('call', [call(602_000)], null), side('put', [put(602_000)], null));
    expect.soft(none[0], 'marks nothing when a side has no money strike, and a money strike on a side that does not list it marks that side only where it lists it (1 of 2)').toMatchObject({ callMoney: false, putMoney: false });

    // The puts' money is 601, a strike only the calls list: no put sits on it, so no put is marked.
    const calls = side('call', [call(601_000), call(602_000)], 602_000);
    const puts = side('put', [put(602_000)], 601_000);
    expect.soft(strikeRows(calls, puts).map((row) => [row.thousandths, row.callMoney, row.putMoney]), 'marks nothing when a side has no money strike, and a money strike on a side that does not list it marks that side only where it lists it (2 of 2)').toEqual([
      [601_000, false, false],
      [602_000, true, false],
    ]);
  });
});

describe('the rows of the table, and what an empty cell may say', () => {
  const call = (thousandths: number, changes: Partial<ChainContract> = {}) => ({ ...contract({ right: 'call', thousandths, state: 'did_not_trade' }), ...changes });
  const put = (thousandths: number) => contract({ right: 'put', thousandths, state: 'did_not_trade' });
  /** A contract of another root at a strike that a standard contract also has: the same strike, another symbol. */
  const odd = (thousandths: number) => ({ ...call(thousandths, { standard: false }), symbol: 'SPY1261016C00602000' });

  it('says a strike is not listed on a side only when the answer proves it, and holds a side that shows none to nothing', () => {
    const shownAll = strikeRows(side('call', [call(601_000)]), side('put', [put(602_000)]));
    expect.soft(shownAll.map((row) => [row.thousandths, row.callGap, row.putGap]), 'says a strike is not listed on a side only when the answer proves it: the side lists nothing, shows all it lists, or the strike is among those it shows (1 of 2)').toEqual([
      [601_000, null, 'not_listed'],
      [602_000, 'not_listed', null],
    ]);

    // Each side lists far more than it shows, so a strike beyond its shown strikes is not something the answer can speak to.
    const calls = side('call', [call(600_000), call(601_000), call(603_000), call(604_000)], 602_000, { listed: 120 });
    const puts = side('put', [put(601_000), put(602_000), put(603_000)], 602_000, { listed: 118 });
    expect.soft(strikeRows(calls, puts).map((row) => [row.thousandths, row.callGap, row.putGap]), 'says a strike is not listed on a side only when the answer proves it: the side lists nothing, shows all it lists, or the strike is among those it shows (2 of 2)').toEqual([
      [600_000, null, 'outside'],
      [601_000, null, null],
      [602_000, 'not_listed', null],
      [603_000, null, null],
      [604_000, null, 'outside'],
    ]);

    const rows = strikeRows(side('call', [call(601_000)]), side('put', [], null, { listed: 5, standard_listed: 0 }));

    expect.soft(rows.map((row) => [row.callGap, row.putGap]), 'holds a side that lists contracts and shows none to nothing: it is not said to list nothing at any strike').toEqual([[null, 'outside']]);

    const nothingListed = strikeRows(side('call', [call(601_000), call(602_000)]), side('put', [], null));

    expect.soft(nothingListed.map((row) => row.putGap), 'says of a side that lists nothing at all that every strike is not listed on it').toEqual(['not_listed', 'not_listed']);
  });

  it('draws two contracts of one right at one strike as two rows with their own keys, and marks the money once', () => {
    const calls = side('call', [call(602_000), odd(602_000)], 602_000);
    const puts = side('put', [put(602_000)], 602_000);

    const rows = strikeRows(calls, puts);

    expect.soft(rows.map((row) => [row.key, row.call?.symbol, row.put?.symbol, row.callGap, row.putGap]), 'draws two contracts of one right at one strike as two rows with their own keys, and the put beside the first is not said to be missing from the second (1 of 2)').toEqual([
      ['602000:0', 'SPY261016C00602000', 'SPY261016P00602000', null, null],
      ['602000:1', 'SPY1261016C00602000', undefined, null, 'no_other'],
    ]);
    expect.soft(new Set(rows.map((row) => row.key)).size, 'draws two contracts of one right at one strike as two rows with their own keys, and the put beside the first is not said to be missing from the second (2 of 2)').toBe(2);

    const moneyOnce = strikeRows(side('call', [call(602_000), odd(602_000)], 602_000), side('put', [put(602_000)], 602_000));

    expect.soft(moneyOnce.map((row) => [row.callMoney, row.putMoney]), 'marks the money once, on the first row of its strike').toEqual([
      [true, true],
      [false, false],
    ]);

    const twoPuts = strikeRows(side('call', [call(602_000)], 602_000), side('put', [put(602_000), { ...put(602_000), symbol: 'SPY1261016P00602000', standard: false }], 602_000));

    expect.soft(twoPuts.map((row) => [row.key, row.callGap, row.putGap]), 'puts a put with two contracts at a strike beside the call, then below it with the call said to have no other (1 of 2)').toEqual([
      ['602000:0', null, null],
      ['602000:1', 'no_other', null],
    ]);
    // The money of both sides is at 602, and is marked on the first row alone, whichever side has the second contract.
    expect.soft(twoPuts.map((row) => [row.callMoney, row.putMoney]), 'puts a put with two contracts at a strike beside the call, then below it with the call said to have no other (2 of 2)').toEqual([
      [true, true],
      [false, false],
    ]);
  });
});

describe('what the body of a chain comes to', () => {
  const noMoney = (changes: Partial<ChainExpiry> = {}) =>
    chainExpiry({ calls: sideWithoutMoney('call', 120, 118), puts: sideWithoutMoney('put', 118, 116), underlying_price: underlyingPrice({ state: 'no_bar_yet', price: null, price_time_ms: null }), ...changes });

  it('says why no strikes are drawn, and whether a later time would help', () => {
    expect.soft(bodyOf(chainExpiry({ calls: side('call', [], null), puts: side('put', [], null) })), 'says nothing is listed only when both sides count none listed').toEqual({ kind: 'nothing_listed' });

    expect.soft(bodyOf(noMoney({ at_ms: SESSION_OPEN_MS })), 'offers a later time only when the session runs past the time asked for (1 of 2)').toMatchObject({ why: { laterTime: true } });
    expect.soft(bodyOf(noMoney({ at_ms: SESSION_CLOSE_MS })), 'offers a later time only when the session runs past the time asked for (2 of 2)').toMatchObject({ why: { laterTime: false } });

    const noLadder = bodyOf(chainExpiry({ calls: sideWithoutMoney('call', 4, 0), puts: sideWithoutMoney('put', 4, 0) }));

    expect.soft(noLadder, 'says a side has no standard contract to centre on when the underlying has a price and a side that lists contracts has no money strike').toEqual({ kind: 'no_strikes', why: { kind: 'no_ladder' } });

    const emptyWindow = bodyOf(chainExpiry({ calls: side('call', [], 602_000, { listed: 4 }), puts: side('put', [], 602_000, { listed: 4 }) }));

    expect.soft(emptyWindow, 'says only that the window is empty when the money is there and nothing is in the window').toEqual({ kind: 'no_strikes', why: { kind: 'empty_window' } });

    const otherSideEmpty = bodyOf(chainExpiry({ calls: side('call', [], null), puts: side('put', [], 602_000, { listed: 4 }) }));

    expect.soft(otherSideEmpty, 'does not take a side that lists nothing for one with no standard contract to centre on').toEqual({ kind: 'no_strikes', why: { kind: 'empty_window' } });
  });

  it.each([
    ['no bar has closed yet', underlyingPrice({ state: 'no_bar_yet', price: null, price_time_ms: null }), { kind: 'no_bar_yet' }],
    ['its bars are not stored', underlyingPrice({ state: 'not_stored', price: null, price_time_ms: null }), { kind: 'not_stored' }],
    ['its bars cannot be read', underlyingPrice({ state: 'unreadable', price: null, price_time_ms: null, problem: 'damaged' }), { kind: 'unreadable', problem: 'damaged' }],
    ['it has a price state this page does not know', underlyingPrice({ state: 'stale' as UnderlyingPrice['state'], price: null, price_time_ms: null }), { kind: 'unknown', code: 'stale' }],
    ['it says it is priced and sent no price', underlyingPrice({ price: null }), { kind: 'incomplete', missing: 'price' }],
  ])('says, with contracts listed and none drawn, that the underlying is the reason when %s, and never that nothing is listed', (_name, price, underlying) => {
    const body = bodyOf(noMoney({ underlying_price: price }));

    expect(body).toEqual({ kind: 'no_strikes', why: { kind: 'underlying', underlying, laterTime: false } });
  });

  it('is the rows when a side shows contracts, and names only a side that lists contracts and shows none', () => {
    const body = bodyOf(chainExpiry({ calls: side('call', [contract({ right: 'call', thousandths: 602_000, state: 'did_not_trade' })]), puts: sideWithoutMoney('put', 3, 0) }));

    expect.soft(body, 'is the rows when a side shows contracts, and names a side that lists contracts and shows none (1 of 2)').toMatchObject({ kind: 'rows', undrawn: ['puts'] });
    expect.soft(body.kind === 'rows' ? body.rows.length : 0, 'is the rows when a side shows contracts, and names a side that lists contracts and shows none (2 of 2)').toBe(1);

    const nothingListed = bodyOf(chainExpiry({ calls: side('call', [contract({ right: 'call', thousandths: 602_000, state: 'did_not_trade' })]), puts: side('put', [], null) }));

    expect.soft(nothingListed, 'does not name a side that lists nothing as one that shows nothing').toMatchObject({ kind: 'rows', undrawn: [] });
  });
});

describe('what one contract cell says', () => {
  it('is what a contract shows: a price with its time, an incomplete price, the state of a contract that traded no price, and a state this page does not know', () => {
    expect.soft(cellOf(contract({ right: 'call', thousandths: 602_000, state: 'traded', price: '2.10', timeMs: 1_791_575_940_000 })), 'is a price with the time of the trade it is the price of').toEqual({ kind: 'traded', price: '2.10', timeMs: 1_791_575_940_000 });

    const traded = contract({ right: 'call', thousandths: 602_000, state: 'traded' });

    expect.soft(cellOf({ ...traded, price_time_ms: null }), 'is never a price when the server left out the time of the trade, or the price of a trade (1 of 2)').toEqual({ kind: 'incomplete', missing: 'time' });
    expect.soft(cellOf({ ...traded, price: null }), 'is never a price when the server left out the time of the trade, or the price of a trade (2 of 2)').toEqual({ kind: 'incomplete', missing: 'price' });

    const stray = { price: '9.99', price_time_ms: 1_791_575_940_000 };

    expect.soft(cellOf({ ...contract({ right: 'call', thousandths: 1, state: 'did_not_trade' }), ...stray }), 'is not a price for a contract that did not trade, that has no prices stored, or whose prices cannot be read, even if a price came with it (1 of 3)').toEqual({ kind: 'did_not_trade' });
    expect.soft(cellOf({ ...contract({ right: 'call', thousandths: 1, state: 'not_stored' }), ...stray }), 'is not a price for a contract that did not trade, that has no prices stored, or whose prices cannot be read, even if a price came with it (2 of 3)').toEqual({ kind: 'not_stored' });
    expect.soft(cellOf({ ...contract({ right: 'call', thousandths: 1, state: 'unreadable', problem: 'damaged' }), ...stray }), 'is not a price for a contract that did not trade, that has no prices stored, or whose prices cannot be read, even if a price came with it (3 of 3)').toEqual({ kind: 'unreadable', problem: 'damaged' });

    const odd = { ...contract({ right: 'call', thousandths: 1, state: 'traded' }), price_state: 'halted' as ChainContract['price_state'] };

    expect.soft(cellOf(odd), 'says in words the code of a price state it does not know, and draws no price even when one came with it').toEqual({ kind: 'unknown', code: 'halted' });
  });

  it('is what the underlying shows: a price with its time, the reason it has none, and a state this page does not know', () => {
    const odd = underlyingPrice({ state: 'stale' as UnderlyingPrice['state'] });

    expect.soft(underlyingViewOf(odd), 'says in words the code of an underlying state it does not know, and draws no price even when one came with it').toEqual({ kind: 'unknown', code: 'stale' });

    expect.soft(underlyingViewOf(underlyingPrice()), 'is the same for the underlying: a price with its time, or the reason it has none (1 of 6)').toEqual({ kind: 'priced', price: '602.40', timeMs: SESSION_CLOSE_MS });
    expect.soft(underlyingViewOf(underlyingPrice({ price_time_ms: null })), 'is the same for the underlying: a price with its time, or the reason it has none (2 of 6)').toEqual({ kind: 'incomplete', missing: 'time' });
    expect.soft(underlyingViewOf(underlyingPrice({ price: null })), 'is the same for the underlying: a price with its time, or the reason it has none (3 of 6)').toEqual({ kind: 'incomplete', missing: 'price' });
    expect.soft(underlyingViewOf(underlyingPrice({ state: 'no_bar_yet', price: null, price_time_ms: null })), 'is the same for the underlying: a price with its time, or the reason it has none (4 of 6)').toEqual({ kind: 'no_bar_yet' });
    expect.soft(underlyingViewOf(underlyingPrice({ state: 'not_stored', price: null, price_time_ms: null })), 'is the same for the underlying: a price with its time, or the reason it has none (5 of 6)').toEqual({ kind: 'not_stored' });
    expect.soft(underlyingViewOf(underlyingPrice({ state: 'unreadable', price: null, price_time_ms: null, problem: 'damaged' })), 'is the same for the underlying: a price with its time, or the reason it has none (6 of 6)').toEqual({ kind: 'unreadable', problem: 'damaged' });
  });
});

describe('what a download would fetch', () => {
  const stored = (right: 'call' | 'put', thousandths: number) => contract({ right, thousandths, state: 'traded' });
  const missing = (right: 'call' | 'put', thousandths: number) => contract({ right, thousandths, state: 'not_stored' });

  const allStored = () => chainExpiry({ calls: side('call', [stored('call', 602_000), contract({ right: 'call', thousandths: 603_000, state: 'did_not_trade' })]), puts: side('put', [stored('put', 602_000)]) });

  it('is what a download would fetch from what is on screen: nothing, the listing, the underlying bars, or the contracts not stored', () => {
    expect.soft(downloadPlan(chainDay(), allStored()), 'is nothing when everything on screen is stored (1 of 2)').toBeNull();
    expect.soft(downloadPlan(null, null), 'is nothing when everything on screen is stored (2 of 2)').toBeNull();

    expect.soft(downloadPlan(chainDay({ listing_state: 'not_stored', expiries: [] }), null), 'is the listing when it is not stored, and the underlying is then not known to be missing').toEqual({ listing: true, underlying: 'unknown', contracts: [], remaining: 0, callTakes: null });

    expect.soft(downloadPlan(chainDay({ listing_state: 'unreadable', expiries: [] }), null), 'is not offered for a listing that is stored but cannot be read, nor for contracts whose prices cannot be read (1 of 2)').toBeNull();
    const unreadable = chainExpiry({ calls: side('call', [contract({ right: 'call', thousandths: 602_000, state: 'unreadable' })]), puts: side('put', []) });
    expect.soft(downloadPlan(chainDay(), unreadable), 'is not offered for a listing that is stored but cannot be read, nor for contracts whose prices cannot be read (2 of 2)').toBeNull();

    const chain = { ...allStored(), underlying_price: underlyingPrice({ state: 'not_stored', price: null, price_time_ms: null }) };

    expect.soft(downloadPlan(chainDay(), chain), 'is the underlying bars when the chain says they are not stored').toEqual({ listing: false, underlying: 'missing', contracts: [], remaining: 0, callTakes: null });

    const unstored = chainExpiry({
      calls: side('call', [stored('call', 601_000), missing('call', 602_000), missing('call', 603_000)]),
      puts: side('put', [missing('put', 601_000), stored('put', 602_000), missing('put', 604_000)]),
    });

    expect.soft(downloadPlan(chainDay(), unstored), 'is exactly the contracts on screen whose prices are not stored, calls then puts, by the symbols the server gave').toEqual({
      listing: false,
      underlying: 'held',
      contracts: ['SPY261016C00602000', 'SPY261016C00603000', 'SPY261016P00601000', 'SPY261016P00604000'],
      remaining: 0,
      callTakes: null,
    });

    const odd = { ...missing('call', 650_000), standard: false, steps_from_the_money: null };
    const withOdd = chainExpiry({ calls: side('call', [odd]), puts: side('put', [contract({ right: 'put', thousandths: 650_000, state: 'did_not_trade' })]) });

    expect.soft(downloadPlan(chainDay(), withOdd)?.contracts, 'counts a contract not standard like any other, and leaves out one that is only not read').toEqual(['SPY261016C00650000']);
  });

  describe('when the server has said how many contracts one call takes', () => {
    const fiveMissing = chainExpiry({
      calls: side('call', [missing('call', 601_000), missing('call', 602_000), missing('call', 603_000)]),
      puts: side('put', [missing('put', 601_000), missing('put', 602_000)]),
    });

    it('names the first as many contracts as one call takes, counts those left for another press, and reads a limit that cannot be right as no limit', () => {
      expect.soft(downloadPlan(chainDay(), fiveMissing, 3), 'names the first as many as one call takes, calls then puts, as the server gave them, and counts those left for another press').toEqual({
        listing: false,
        underlying: 'held',
        contracts: ['SPY261016C00601000', 'SPY261016C00602000', 'SPY261016C00603000'],
        remaining: 2,
        callTakes: 3,
      });

      expect.soft(downloadPlan(chainDay(), fiveMissing, 5), 'names all of them, and none are left, when one call takes as many as are missing or more (1 of 3)').toMatchObject({ remaining: 0, callTakes: 5 });
      expect.soft(downloadPlan(chainDay(), fiveMissing, 5)?.contracts.length, 'names all of them, and none are left, when one call takes as many as are missing or more (2 of 3)').toBe(5);
      expect.soft(downloadPlan(chainDay(), fiveMissing, 60), 'names all of them, and none are left, when one call takes as many as are missing or more (3 of 3)').toMatchObject({ remaining: 0, callTakes: 60 });

      expect.soft(downloadPlan(chainDay(), fiveMissing, 1), 'takes one contract at a time when one call takes one, and leaves the rest').toMatchObject({ contracts: ['SPY261016C00601000'], remaining: 4 });

      expect.soft(downloadPlan(chainDay(), fiveMissing, 0), 'takes a limit that cannot be right (none, or less than one) for no limit, and names every contract (1 of 2)').toMatchObject({ remaining: 0, callTakes: null });
      expect.soft(downloadPlan(chainDay(), fiveMissing, 0)?.contracts.length, 'takes a limit that cannot be right (none, or less than one) for no limit, and names every contract (2 of 2)').toBe(5);
    });
  });

  describe('when the lake will fetch nothing for the session yet', () => {
    const refused = { reason: 'session_not_over', words: 'The session of 2026-10-09 is not over in New York yet.' } as const;

    it('offers nothing whatever is missing, and is offered as before when the refusal is null or absent', () => {
      const chain = chainExpiry({ calls: side('call', [missing('call', 602_000)]), puts: side('put', []), underlying_price: underlyingPrice({ state: 'not_stored', price: null, price_time_ms: null }) });

      expect.soft(downloadPlan(chainDay({ listing_state: 'not_stored', expiries: [], fetch_refusal: refused }), chain), 'offers nothing, whatever is missing: not the listing, not the underlying, not a contract (1 of 2)').toBeNull();
      expect.soft(downloadPlan(chainDay({ fetch_refusal: refused }), chain, 60), 'offers nothing, whatever is missing: not the listing, not the underlying, not a contract (2 of 2)').toBeNull();

      expect.soft(downloadPlan(chainDay({ listing_state: 'not_stored', expiries: [], fetch_refusal: null }), null), 'is offered as before when the refusal is null or absent (1 of 2)').not.toBeNull();
      expect.soft(downloadPlan(chainDay({ listing_state: 'not_stored', expiries: [] }), null), 'is offered as before when the refusal is null or absent (2 of 2)').not.toBeNull();
    });
  });

  it('says in words what it will fetch, and how many contracts one call takes', () => {
    const base = { remaining: 0, callTakes: null };
    expect.soft(planWords({ ...base, listing: true, underlying: 'missing', contracts: ['a', 'b', 'c'] }), 'says in words what it will fetch: the listing and the bars as a list, the contracts as a label and a number (1 of 6)').toBe("Fetches the listing and the underlying's bars. Contracts to fetch: 3.");
    expect.soft(planWords({ ...base, listing: false, underlying: 'held', contracts: ['a'] }), 'says in words what it will fetch: the listing and the bars as a list, the contracts as a label and a number (2 of 6)').toBe('Contracts to fetch: 1.');
    expect.soft(planWords({ ...base, listing: false, underlying: 'missing', contracts: ['a', 'b'] }), 'says in words what it will fetch: the listing and the bars as a list, the contracts as a label and a number (3 of 6)').toBe("Fetches the underlying's bars. Contracts to fetch: 2.");
    expect.soft(planWords({ ...base, listing: true, underlying: 'unknown', contracts: [] }), 'says in words what it will fetch: the listing and the bars as a list, the contracts as a label and a number (4 of 6)').toBe("Fetches the listing and the underlying's bars if they are missing.");
    expect.soft(planWords({ ...base, listing: true, underlying: 'held', contracts: [] }), 'says in words what it will fetch: the listing and the bars as a list, the contracts as a label and a number (5 of 6)').toBe('Fetches the listing.');
    expect.soft(planWords({ ...base, listing: false, underlying: 'held', contracts: [] }), 'says in words what it will fetch: the listing and the bars as a list, the contracts as a label and a number (6 of 6)').toBe('Nothing is missing.');

    expect.soft(planWords({ listing: false, underlying: 'held', contracts: ['a', 'b'], remaining: 0, callTakes: 60 }), 'says how many contracts one call takes when it knows, and how many will still be missing after this call (1 of 2)').toBe('Contracts to fetch: 2. The most contracts one call takes: 60.');
    expect.soft(planWords({ listing: false, underlying: 'held', contracts: ['a', 'b'], remaining: 3, callTakes: 2 }), 'says how many contracts one call takes when it knows, and how many will still be missing after this call (2 of 2)').toBe('Contracts to fetch in this call: 2. Still missing after it: 3. Press again for them. The most contracts one call takes: 2.');
  });

  it('is a request of the underlying, the trading date, and the contracts, and names no expiry when it names no contract', () => {
    const plan = { listing: false, underlying: 'held' as const, contracts: ['SPY261016C00602000'], remaining: 0, callTakes: null };

    expect.soft(downloadRequest(plan, 'SPY', TRADING_DATE_MS, EXPIRY_MS['2026-10-16']), 'is a request of the underlying, the trading date, and the contracts, with the expiry only to check them against').toEqual({
      underlying: 'SPY',
      trading_date_ms: 1_791_518_400_000,
      expiry_ms: 1_792_180_800_000,
      contracts: ['SPY261016C00602000'],
    });

    const request = downloadRequest({ listing: true, underlying: 'unknown', contracts: [], remaining: 0, callTakes: null }, 'SPY', TRADING_DATE_MS, EXPIRY_MS['2026-10-16']);

    expect.soft(request, 'names no expiry when it names no contract (1 of 2)').toEqual({ underlying: 'SPY', trading_date_ms: TRADING_DATE_MS, contracts: [] });
    expect.soft('expiry_ms' in request, 'names no expiry when it names no contract (2 of 2)').toBe(false);
  });
});

describe('what an answer of a download comes to', () => {
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
