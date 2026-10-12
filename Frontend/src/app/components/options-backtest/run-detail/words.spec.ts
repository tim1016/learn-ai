import { describe, expect, it } from 'vitest';

import { optionContractName } from '../../../shared/option-contract/option-contract.component';
import { CALL_585, EXPIRY_MS, PUT_585 } from './testing/detail-fixtures';
import { attemptWords, contractOf, FILL_WORDS, LEVEL_WORDS, legAttempt, MARK_WORDS } from './words';

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
