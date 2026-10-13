import { describe, expect, it } from 'vitest';

import { optionContractName } from '../../../shared/option-contract/option-contract.component';
import { CALL_585, EXPIRY_MS, PUT_585 } from './testing/detail-fixtures';
import { plainTrade, stoppedTrade } from './testing/detail-fixtures';
import { owedTrade, soldTrade } from './testing/selling-fixtures';
import {
  attemptWords, closedBecauseHeading, contractOf, fillWords, FILL_WORDS, isSoldLegFill, isSoldLegTrade, LEVEL_WORDS, legAttempt, markWords, MARK_WORDS,
  marksSection, OWED_MARK_WORDS, SOLD_FILL_WORDS,
} from './words';

describe('a contract as the shared renderer takes it', () => {
  it('carries the fields the server sent, renamed and untouched: the strike is the server’s text, and the symbol is not read', () => {
    expect(contractOf({ ...CALL_585, strike_usd: '585.050' })).toEqual({
      occ_symbol: 'SPY261016C00585000',
      underlying: 'SPY',
      expiry_ms: EXPIRY_MS,
      right: 'call',
      strike: '585.050',
    });
  });

  it('takes the right and the strike from their own fields even when the symbol says otherwise', () => {
    const odd = { ...PUT_585, symbol: 'SPY261016C00999000' };

    expect(contractOf(odd)).toMatchObject({ right: 'put', strike: '585.00', occ_symbol: 'SPY261016C00999000' });
  });

  it('is named in words by the shared helper, with the expiry as a New York date', () => {
    expect(optionContractName(contractOf(CALL_585))).toBe("SPY Oct 16 '26 585 Call");
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
  it('gives each kind of line its own way of being drawn, so that colour is never the only difference', () => {
    const drawn = Object.values(LEVEL_WORDS).map((level) => level.drawn);

    expect(new Set(drawn).size).toBe(3);
    expect(LEVEL_WORDS.starting_price).toEqual({ name: 'Starting price', drawn: 'dotted line' });
    expect(LEVEL_WORDS.momentum).toEqual({ name: 'Momentum level', drawn: 'dashed line' });
    expect(LEVEL_WORDS.stop).toEqual({ name: 'Stop level', drawn: 'long dashes' });
  });

  it('tells a purchase, a sale and a mark apart in words and in shape', () => {
    expect([FILL_WORDS.entry, FILL_WORDS.exit, MARK_WORDS].map((each) => each.name)).toEqual(['Bought', 'Sold', 'Marked, not sold']);
    expect(new Set([FILL_WORDS.entry.drawn, FILL_WORDS.exit.drawn, MARK_WORDS.drawn]).size).toBe(3);
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

  it('gives a bought leg the words it always had, from the same constants', () => {
    expect(fillWords({ kind: 'entry', side: 'buy' })).toBe(FILL_WORDS.entry);
    expect(fillWords({ kind: 'exit', side: 'sell' })).toBe(FILL_WORDS.exit);
    expect(fillWords({ kind: 'entry', side: 'sell' })).toBe(SOLD_FILL_WORDS.entry);
    expect(fillWords({ kind: 'exit', side: 'buy' })).toBe(SOLD_FILL_WORDS.exit);
  });

  it('never reads a sold leg’s words as a bought leg’s: the four names are four', () => {
    const names = [FILL_WORDS.entry, FILL_WORDS.exit, SOLD_FILL_WORDS.entry, SOLD_FILL_WORDS.exit].map((each) => each.name);

    expect(new Set(names).size).toBe(4);
    // An arrow points the way it traded: purchases up, sales down, whichever opened the leg.
    expect([FILL_WORDS.entry, SOLD_FILL_WORDS.exit].map((each) => each.drawn)).toEqual(['up arrow', 'up arrow']);
    expect([FILL_WORDS.exit, SOLD_FILL_WORDS.entry].map((each) => each.drawn)).toEqual(['down arrow', 'down arrow']);
  });

  it('knows a fill is of a sold leg from the way it traded, and never asks the leg', () => {
    expect([isSoldLegFill({ kind: 'entry', side: 'sell' }), isSoldLegFill({ kind: 'exit', side: 'buy' })]).toEqual([true, true]);
    expect([isSoldLegFill({ kind: 'entry', side: 'buy' }), isSoldLegFill({ kind: 'exit', side: 'sell' })]).toEqual([false, false]);
  });

  it('knows a trade is of a sold leg from its entry', () => {
    expect([isSoldLegTrade(soldTrade()), isSoldLegTrade(owedTrade())]).toEqual([true, true]);
    expect([isSoldLegTrade(stoppedTrade()), isSoldLegTrade(plainTrade())]).toEqual([false, false]);
  });

  it('says a bought leg’s mark is not sold and a sold leg’s is owed, tells them apart in words and keeps the same shape', () => {
    expect(markWords({ owed: false })).toBe(MARK_WORDS);
    expect(markWords({ owed: true })).toBe(OWED_MARK_WORDS);
    expect([MARK_WORDS.name, OWED_MARK_WORDS.name]).toEqual(['Marked, not sold', 'Marked, owed']);
    expect(MARK_WORDS.drawn).toBe(OWED_MARK_WORDS.drawn);
  });

  it('heads the reason of a close "Sold because" until a buy-back is among the fills', () => {
    expect(closedBecauseHeading([])).toBe('Sold because');
    expect(closedBecauseHeading([{ kind: 'entry', side: 'buy' }, { kind: 'exit', side: 'sell' }])).toBe('Sold because');
    expect(closedBecauseHeading([{ kind: 'entry', side: 'sell' }])).toBe('Closed because');
  });

  it('names the section of marks for what it holds', () => {
    expect(marksSection([]).heading).toBe('Marked, not sold');
    expect(marksSection([{ owed: false }]).heading).toBe('Marked, not sold');
    expect(marksSection([{ owed: true }, { owed: true }]).heading).toBe('Marked, owed');
    expect(marksSection([{ owed: false }, { owed: true }]).heading).toBe('Marked, not sold or owed');
  });
});
