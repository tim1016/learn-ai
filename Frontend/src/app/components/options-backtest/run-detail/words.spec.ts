import { describe, expect, it } from 'vitest';

import { optionContractName } from '../../../shared/option-contract/option-contract.component';
import { CALL_585, EXPIRY_MS, PUT_585 } from './testing/detail-fixtures';
import { plainTrade, stoppedTrade } from './testing/detail-fixtures';
import { owedTrade, soldTrade } from './testing/selling-fixtures';
import {
  attemptWords, closedBecauseHeading, contractOf, fillWords, FILL_WORDS, isSoldLegFill, isSoldLegTrade, LEVEL_WORDS, legAttempt, markWords, MARK_WORDS,
  marksSection, OWED_MARK_WORDS,
} from './words';

describe('a contract as the shared renderer takes it', () => {
  it('carries the fields the server sent to the shared renderer, takes the right and the strike from their own fields, and is named in words by the shared helper', () => {
    expect.soft(contractOf({ ...CALL_585, strike_usd: '585.050' }), 'carries the fields the server sent, renamed and untouched: the strike is the server’s text, and the symbol is not read').toEqual({
      occ_symbol: 'SPY261016C00585000',
      underlying: 'SPY',
      expiry_ms: EXPIRY_MS,
      right: 'call',
      strike: '585.050',
    });

    const odd = { ...PUT_585, symbol: 'SPY261016C00999000' };
    expect.soft(contractOf(odd), 'takes the right and the strike from their own fields even when the symbol says otherwise').toMatchObject({ right: 'put', strike: '585.00', occ_symbol: 'SPY261016C00999000' });

    expect.soft(optionContractName(contractOf(CALL_585)), 'is named in words by the shared helper, with the expiry as a New York date').toBe("SPY Oct 16 '26 585 Call");
  });
});

describe('which try a leg is on', () => {
  it.each([
    [0, 'first entry'],
    [1, 're-entry 1'],
    [2, 're-entry 2'],
    [7, 're-entry 7'],
  ])('reads entry number %i as %s', (entryNumber, words) => {
    expect(attemptWords(entryNumber)).toBe(words);
  });

  it('names the leg first', () => {
    expect(legAttempt('call', 0)).toBe('call, first entry');
    expect(legAttempt('put_2', 1)).toBe('put_2, re-entry 1');
  });
});

describe('what the lines and marks are called', () => {
  it('gives each kind of line its own way of being drawn, and tells a purchase, a sale and a mark apart in words and in shape', () => {
    const drawn = Object.values(LEVEL_WORDS).map((level) => level.drawn);

    expect.soft(new Set(drawn).size, 'gives each kind of line its own way of being drawn, so that colour is never the only difference').toBe(3);
    expect.soft(LEVEL_WORDS.starting_price, 'calls a starting price a dotted line').toEqual({ name: 'Starting price', drawn: 'dotted line' });
    expect.soft(LEVEL_WORDS.momentum, 'calls a momentum level a dashed line').toEqual({ name: 'Momentum level', drawn: 'dashed line' });
    expect.soft(LEVEL_WORDS.stop, 'calls a stop level long dashes').toEqual({ name: 'Stop level', drawn: 'long dashes' });
    expect.soft([FILL_WORDS.entry, FILL_WORDS.exit, MARK_WORDS].map((each) => each.name), 'tells a purchase, a sale and a mark apart in words').toEqual(['Bought', 'Sold', 'Marked, not sold']);
    expect.soft(new Set([FILL_WORDS.entry.drawn, FILL_WORDS.exit.drawn, MARK_WORDS.drawn]).size, 'tells a purchase, a sale and a mark apart in shape').toBe(3);
  });
});

describe('what a fill and a mark are called, by the side they traded on (#3109)', () => {
  it.each([
    ['entry', 'buy', 'Bought', 'up arrow'],
    ['exit', 'sell', 'Sold', 'down arrow'],
    ['entry', 'sell', 'Sold to open', 'down arrow'],
    ['exit', 'buy', 'Bought back', 'up arrow'],
  ] as const)('calls a %s on the %s side %s and draws it as an %s', (kind, side, name, drawn) => {
    expect(fillWords({ kind, side })).toEqual({ name, drawn });
  });

  it('knows a fill and a trade are of a sold leg from the way they traded, tells a bought leg’s mark from a sold leg’s, and heads the reason of a close and the section of marks for what they hold', () => {
    expect.soft([isSoldLegFill({ kind: 'entry', side: 'sell' }), isSoldLegFill({ kind: 'exit', side: 'buy' })], 'knows a fill is of a sold leg from the way it traded, and never asks the leg (a sale to open, a buy-back)').toEqual([true, true]);
    expect.soft([isSoldLegFill({ kind: 'entry', side: 'buy' }), isSoldLegFill({ kind: 'exit', side: 'sell' })], 'knows a fill is of a bought leg from the way it traded, and never asks the leg (a purchase, a sale)').toEqual([false, false]);

    expect.soft([isSoldLegTrade(soldTrade()), isSoldLegTrade(owedTrade())], 'knows a trade is of a sold leg from its entry (sold, owed)').toEqual([true, true]);
    expect.soft([isSoldLegTrade(stoppedTrade()), isSoldLegTrade(plainTrade())], 'knows a trade is of a bought leg from its entry (stopped, plain)').toEqual([false, false]);

    expect.soft(markWords({ owed: false }), 'says a bought leg’s mark is not sold').toBe(MARK_WORDS);
    expect.soft(markWords({ owed: true }), 'says a sold leg’s mark is owed').toBe(OWED_MARK_WORDS);
    expect.soft([MARK_WORDS.name, OWED_MARK_WORDS.name], 'tells a bought leg’s mark and a sold leg’s apart in words').toEqual(['Marked, not sold', 'Marked, owed']);
    expect.soft(MARK_WORDS.drawn, 'keeps the same shape for a bought leg’s mark and a sold leg’s').toBe(OWED_MARK_WORDS.drawn);

    expect.soft(closedBecauseHeading([]), 'heads the reason of a close "Sold because" until a buy-back is among the fills (no fills)').toBe('Sold because');
    expect.soft(closedBecauseHeading([{ kind: 'entry', side: 'buy' }, { kind: 'exit', side: 'sell' }]), 'heads the reason of a close "Sold because" until a buy-back is among the fills (a purchase and a sale)').toBe('Sold because');
    expect.soft(closedBecauseHeading([{ kind: 'entry', side: 'sell' }]), 'heads the reason of a close "Closed because" once a sold leg is among the fills').toBe('Closed because');

    expect.soft(marksSection([]).heading, 'names the section of marks for what it holds (no marks)').toBe('Marked, not sold');
    expect.soft(marksSection([{ owed: false }]).heading, 'names the section of marks for what it holds (a mark not sold)').toBe('Marked, not sold');
    expect.soft(marksSection([{ owed: true }, { owed: true }]).heading, 'names the section of marks for what it holds (marks owed)').toBe('Marked, owed');
    expect.soft(marksSection([{ owed: false }, { owed: true }]).heading, 'names the section of marks for what it holds (both)').toBe('Marked, not sold or owed');
  });
});
