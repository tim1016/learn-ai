import { describe, expect, it } from 'vitest';

import { chosenStrikes, mostStrikesText } from './past-chain-controls';

describe('the most strikes the server takes, as the page holds it', () => {
  it('is the digits of the number the server sent, and nothing while the limits are not known', () => {
    expect(mostStrikesText({ max_strikes_each_side: 14 })).toBe('14');
    expect(mostStrikesText({ max_strikes_each_side: 9 })).toBe('9');
    expect(mostStrikesText(null)).toBeNull();
  });

  it('is nothing for a number that cannot be written as digits, so no count is refused on a limit the page cannot read', () => {
    expect(mostStrikesText({ max_strikes_each_side: -1 })).toBeNull();
    expect(mostStrikesText({ max_strikes_each_side: 1.5 })).toBeNull();
    expect(mostStrikesText({ max_strikes_each_side: 1e21 })).toBeNull();
    expect(mostStrikesText({ max_strikes_each_side: Number.NaN })).toBeNull();
  });
});

describe('the strikes box against the most the server takes', () => {
  it.each([
    ['0', '0'],
    ['1', '1'],
    ['9', '9'],
    ['13', '13'],
    ['14', '14'],
    ['014', '14'],
    ['0000', '0'],
  ])('sends %j as the digits %j: it is not more than 14', (typed, digits) => {
    expect(chosenStrikes(typed, '14')).toEqual({ kind: 'count', digits });
  });

  it.each(['15', '20', '99', '100', '1000', '015', '99999999999999999999'])('refuses %j before anything is asked: it is more than 14', (typed) => {
    expect(chosenStrikes(typed, '14')).toEqual({
      kind: 'refused',
      problem: 'Strikes each side of the money: the most the server takes is 14. Enter 14 or fewer, or leave the box empty.',
    });
  });

  it('judges a count by the limit the server sent, whatever it is', () => {
    expect(chosenStrikes('8', '8')).toEqual({ kind: 'count', digits: '8' });
    expect(chosenStrikes('9', '8')).toMatchObject({ kind: 'refused' });
    expect(chosenStrikes('10', '9')).toMatchObject({ kind: 'refused' });
    expect(chosenStrikes('9', '10')).toEqual({ kind: 'count', digits: '9' });
  });

  it('leaves the judging to the server while it has no limit: any whole number is sent', () => {
    expect(chosenStrikes('15', null)).toEqual({ kind: 'count', digits: '15' });
    expect(chosenStrikes('15')).toEqual({ kind: 'count', digits: '15' });
  });

  it('still says what a box that is not a whole number needs, and that an empty box means the server’s number', () => {
    expect(chosenStrikes('x', '14')).toEqual({ kind: 'refused', problem: 'Enter a whole number of strikes, or leave the box empty.' });
    expect(chosenStrikes('  ', '14')).toEqual({ kind: 'default' });
  });
});
