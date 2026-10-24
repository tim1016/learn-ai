import { describe, expect, it } from 'vitest';

import type { ChainContractCell, ChainField, ChainRow, ChainSideCell } from '../chain-presentation';
import { archiveBodyState, archiveChainState, archiveDayState, archivePresentation } from './archive-chain';
import { bodyOf, chainViewOf, dayViewOf } from './past-chain-view';
import type { ChainContract, ChainExpiry } from './past-chain.types';
import {
  EXPIRY_MS,
  MINUTE_1545_MS,
  MINUTE_1559_MS,
  SESSION_CLOSE_MS,
  chainDay,
  chainExpiry,
  contract,
  side,
  sideWithoutMoney,
  underlyingPrice,
} from './testing/fixtures';

const rowsOf = (chain: ChainExpiry): readonly ChainRow[] => archivePresentation(chain).table.rows;
const contractOf = (cell: ChainSideCell): ChainContractCell => {
  if (cell.kind !== 'contract') throw new Error('The cell holds no contract.');
  return cell;
};
const lastOf = (cell: ChainSideCell): ChainField => contractOf(cell).fields.last;
const rowAt = (rows: readonly ChainRow[], strike: string): ChainRow => {
  const row = rows.find((candidate) => candidate.strike === strike);
  if (row === undefined) throw new Error(`No row of the chain reads ${strike}.`);
  return row;
};

describe('a Past · Archive chain, as the one chain presentation', () => {
  it('draws a contract that traded with its price as the server wrote it and the minute it is the close of', () => {
    const rows = rowsOf(chainExpiry());

    expect(lastOf(rowAt(rows, '$602.00').call)).toEqual({ kind: 'value', text: '$2.10', atMs: MINUTE_1559_MS });
    expect(lastOf(rowAt(rows, '$603.00').put)).toEqual({ kind: 'value', text: '$1.05', atMs: MINUTE_1545_MS });
  });

  it('never draws a value observed after its instant, and says that is why', () => {
    const late = contract({ right: 'call', thousandths: 602_000, state: 'traded', price: '9.99', timeMs: SESSION_CLOSE_MS + 60_000 });
    const chain = chainExpiry({ at_ms: SESSION_CLOSE_MS, calls: side('call', [late]), puts: side('put', []) });

    const field = lastOf(rowAt(rowsOf(chain), '$602.00').call);

    expect(field).toEqual({ kind: 'absent', reason: 'after_instant', detail: null });
    expect(JSON.stringify(archivePresentation(chain))).not.toContain('9.99');
  });

  it('does not draw an underlying price observed after the instant either', () => {
    const chain = chainExpiry({ at_ms: MINUTE_1545_MS, underlying_price: underlyingPrice({ price: '605.00', price_time_ms: MINUTE_1559_MS }) });

    const fact = archivePresentation(chain).facts.find((each) => each.label === 'SPY then');

    expect(fact?.value).toEqual({ kind: 'field', field: { kind: 'absent', reason: 'after_instant', detail: null } });
  });

  it.each([
    ['did not trade', { state: 'did_not_trade' as const }, { reason: 'did_not_trade', detail: null }],
    ['is not stored', { state: 'not_stored' as const }, { reason: 'not_stored', detail: null }],
    ['cannot be read', { state: 'unreadable' as const, problem: 'the stored file is damaged (OptionBarsFileError)' }, { reason: 'unreadable', detail: 'the stored file is damaged (OptionBarsFileError)' }],
  ])('says why a contract that %s has no price', (_name, wanted, expected) => {
    const odd = contract({ right: 'call', thousandths: 602_000, ...wanted });
    const chain = chainExpiry({ calls: side('call', [odd]), puts: side('put', []) });

    expect(lastOf(rowAt(rowsOf(chain), '$602.00').call)).toEqual({ kind: 'absent', ...expected });
  });

  it.each([
    ['price', { price: null }, 'no_price_sent'],
    ['time', { price_time_ms: null }, 'no_time_sent'],
  ] as const)('shows no price for a trade the server sent without its %s', (_missing, changes, reason) => {
    const odd = { ...contract({ right: 'call', thousandths: 602_000, state: 'traded', price: '2.10' }), ...changes };
    const chain = chainExpiry({ calls: side('call', [odd]), puts: side('put', []) });

    const field = lastOf(rowAt(rowsOf(chain), '$602.00').call);

    expect(field).toEqual({ kind: 'absent', reason, detail: null });
  });

  it('says in words a price state it does not know, and draws no price for it', () => {
    const odd = { ...contract({ right: 'call', thousandths: 602_000, state: 'did_not_trade' }), price_state: 'partial' as ChainContract['price_state'] };
    const chain = chainExpiry({ calls: side('call', [odd]), puts: side('put', []) });

    expect(lastOf(rowAt(rowsOf(chain), '$602.00').call)).toEqual({ kind: 'absent', reason: 'unknown_state', detail: 'Partial' });
  });

  it('never fills a quote, a Greek, an open interest or a volume: the archive holds trade bars, so each is absent for its own reason', () => {
    const cell = contractOf(rowAt(rowsOf(chainExpiry()), '$602.00').call);

    expect(cell.fields.last.kind, 'the trade price is the one value').toBe('value');
    for (const quote of ['bid', 'ask', 'spread'] as const) {
      expect.soft(cell.fields[quote], `${quote} is not a last trade`).toEqual({ kind: 'absent', reason: 'no_quote_archived', detail: null });
    }
    for (const recorded of ['delta', 'gamma', 'theta', 'vega', 'volume', 'openInterest'] as const) {
      expect.soft(cell.fields[recorded], `${recorded} needs a recorded snapshot, and none was`).toEqual({ kind: 'absent', reason: 'no_recorded_snapshot', detail: null });
    }
  });

  it('draws one wide price column on each side, and says what the archive cannot say', () => {
    const presentation = archivePresentation(chainExpiry());

    expect(presentation.table.columns).toEqual({ calls: ['last'], puts: ['last'], detail: 'full' });
    expect(presentation.table.centre).toBe(false);
    expect(presentation.notes.join(' ')).toMatch(/last trade, never a bid or an ask/);
    expect(presentation.notes.join(' ')).toMatch(/No current value stands in for a past one/);
  });

  it('states the contract identity of the chain: the underlying, the expiry with its series, and the standard counts as served', () => {
    const presentation = archivePresentation(chainExpiry());

    expect(presentation).toMatchObject({ mode: 'past-archive', underlying: 'SPY', expiryMs: EXPIRY_MS['2026-10-16'] });
    expect(presentation.facts.find((fact) => fact.label === 'Expiry')?.value).toMatchObject({ kind: 'expiry', ms: EXPIRY_MS['2026-10-16'], tag: { series: 'monthly' } });
    expect(presentation.facts.find((fact) => fact.label === 'Contracts')?.value).toEqual({ kind: 'text', text: 'Calls: 3 listed, 3 standard. Puts: 3 listed, 3 standard.' });
  });

  it('labels a contract that is not standard, and keeps it on its strike', () => {
    const odd = contract({ right: 'call', thousandths: 602_500, state: 'did_not_trade', standard: false });
    const chain = chainExpiry({ calls: side('call', [odd]), puts: side('put', []) });

    expect(contractOf(rowAt(rowsOf(chain), '$602.50').call).standard).toBe(false);
  });

  it('draws a strike as the server wrote it, and marks the money apart for calls and puts', () => {
    const chain = chainExpiry({
      calls: side('call', [contract({ right: 'call', thousandths: 602_000, state: 'did_not_trade' }), contract({ right: 'call', thousandths: 603_000, state: 'did_not_trade' })], 602_000),
      puts: side('put', [contract({ right: 'put', thousandths: 602_000, state: 'did_not_trade' }), contract({ right: 'put', thousandths: 603_000, state: 'did_not_trade' })], 603_000),
    });

    const rows = rowsOf(chain);

    expect(rows.map((row) => row.strike)).toEqual(['$602.00', '$603.00']);
    expect(rowAt(rows, '$602.00')).toMatchObject({ atm: true, mark: 'At the money for calls' });
    expect(rowAt(rows, '$603.00')).toMatchObject({ atm: true, mark: 'At the money for puts' });
  });

  it('says a strike is not listed on a side that has no contract at it, and gives the other side its price', () => {
    const chain = chainExpiry({
      calls: side('call', [contract({ right: 'call', thousandths: 601_000, state: 'did_not_trade' })]),
      puts: side('put', [contract({ right: 'put', thousandths: 602_000, state: 'did_not_trade' })]),
    });

    const rows = rowsOf(chain);

    expect(rowAt(rows, '$601.00').put).toEqual({ kind: 'gap', words: 'Not listed at this strike' });
    expect(rowAt(rows, '$602.00').call).toEqual({ kind: 'gap', words: 'Not listed at this strike' });
  });

  it('puts two contracts at one strike on two rows, so none hides behind the other', () => {
    const chain = chainExpiry({
      calls: side('call', [contract({ right: 'call', thousandths: 602_000, state: 'did_not_trade' }), contract({ right: 'call', thousandths: 602_000, state: 'not_stored', standard: false })]),
      puts: side('put', [contract({ right: 'put', thousandths: 602_000, state: 'did_not_trade' })]),
    });

    const rows = rowsOf(chain);

    expect(rows).toHaveLength(2);
    expect(rows[1]?.put).toEqual({ kind: 'gap', words: 'No other contract at this strike' });
  });
});

describe('the states a Past · Archive chain can be in without a grid', () => {
  const fromChain = (chain: ChainExpiry) => archiveChainState(chainViewOf({ dayReady: true, expiryPicked: true, blockedBy: null, status: 'resolved', chain, problem: null }), chain.expiry_ms);

  it('says an expiry the date does not list is not listed, and opens no other', () => {
    const state = fromChain(chainExpiry({ expiry_listed: false, tag: null, calls: sideWithoutMoney('call', 0, 0), puts: sideWithoutMoney('put', 0, 0) }));

    expect(state).toMatchObject({ kind: 'unavailable', happened: 'The 2026-10-16 expiry is not listed on this date.' });
    expect(state?.next).toMatch(/none has been opened for you/);
  });

  it('tells not listed apart from not knowing whether it is listed', () => {
    const state = fromChain(chainExpiry({ expiry_listed: null, tag: null, listing_state: 'unreadable', listing_problem: 'the listing cannot be read (OptionListingFileError)' }));

    expect(state?.happened).toBe('Whether the 2026-10-16 expiry is listed cannot be said: the listing for this date is stored but cannot be read.');
    expect(state?.why).toBe('the listing cannot be read (OptionListingFileError)');
  });

  it('says nothing is listed only when both counts say so', () => {
    const chain = chainExpiry({ calls: sideWithoutMoney('call', 0, 0), puts: sideWithoutMoney('put', 0, 0) });

    expect(archiveBodyState(chain, bodyOf(chain), false)).toMatchObject({ kind: 'idle', happened: 'No contract is listed at this expiry.', why: 'Calls listed: 0. Puts listed: 0.' });
  });

  it.each([
    ['no bar had closed by then', { state: 'no_bar_yet', price: null, price_time_ms: null }, /No bar of the underlying had closed by this time/],
    ['its bars are not stored', { state: 'not_stored', price: null, price_time_ms: null }, /bars for this date are not stored/],
    ['its bars cannot be read', { state: 'unreadable', price: null, price_time_ms: null, problem: 'x' }, /cannot be read, so there is no price/],
  ] as const)('says why no strikes are drawn when %s, and counts what is listed', (_name, price, why) => {
    const chain = chainExpiry({ underlying_price: underlyingPrice(price), calls: sideWithoutMoney('call', 120, 118), puts: sideWithoutMoney('put', 118, 116) });

    const state = archiveBodyState(chain, bodyOf(chain), false);

    expect(state?.why).toMatch(why);
    expect(state?.why).toMatch(/Calls listed: 120\. Puts listed: 118\./);
  });

  it('points to the download for bars that are not stored, only when it is on offer', () => {
    const chain = chainExpiry({ underlying_price: underlyingPrice({ state: 'not_stored', price: null, price_time_ms: null }), calls: sideWithoutMoney('call', 120, 118), puts: sideWithoutMoney('put', 118, 116) });

    expect(archiveBodyState(chain, bodyOf(chain), true)?.next).toMatch(/Download missing data above/);
    expect(archiveBodyState(chain, bodyOf(chain), false)?.next).toBeNull();
  });

  it('says a session that is not a trading day is invalid, with the server words', () => {
    const view = dayViewOf('error', null, { message: '2026-10-10 is a weekend day', code: 'OPTIONS_BACKTEST_NOT_A_SESSION', control: 'date', canRetry: false });

    expect(archiveDayState(view)).toEqual({ kind: 'invalid', happened: 'Not a trading day.', why: '2026-10-10 is a weekend day' });
  });

  it('draws no state for a listing that holds expiries', () => {
    expect(archiveDayState(dayViewOf('resolved', chainDay(), null))).toBeNull();
  });
});
