import { describe, expect, it } from 'vitest';

import { chosenStrikes, mostStrikesText } from './past-chain-controls';

describe('the most strikes the server takes, as the page holds it', () => {
  it('is the digits of the number the server sent, and nothing while the limits are not known or for a number that cannot be written as digits', () => {
    expect.soft(mostStrikesText({ max_strikes_each_side: 14 }), 'is the digits of the number the server sent: 14').toBe('14');
    expect.soft(mostStrikesText({ max_strikes_each_side: 9 }), 'is the digits of the number the server sent: 9').toBe('9');
    expect.soft(mostStrikesText(null), 'is nothing while the limits are not known').toBeNull();
    expect.soft(mostStrikesText({ max_strikes_each_side: -1 }), 'is nothing for -1, so no count is refused on a limit the page cannot read').toBeNull();
    expect.soft(mostStrikesText({ max_strikes_each_side: 1.5 }), 'is nothing for 1.5, so no count is refused on a limit the page cannot read').toBeNull();
    expect.soft(mostStrikesText({ max_strikes_each_side: 1e21 }), 'is nothing for 1e21, so no count is refused on a limit the page cannot read').toBeNull();
    expect.soft(mostStrikesText({ max_strikes_each_side: Number.NaN }), 'is nothing for NaN, so no count is refused on a limit the page cannot read').toBeNull();
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

  it('judges a count by the limit the server sent, whatever it is, leaves the judging to the server while it has no limit, and says what a box that is not a whole number needs', () => {
    expect.soft(chosenStrikes('8', '8'), 'sends 8 when the limit the server sent is 8').toEqual({ kind: 'count', digits: '8' });
    expect.soft(chosenStrikes('9', '8'), 'refuses 9 when the limit the server sent is 8').toMatchObject({ kind: 'refused' });
    expect.soft(chosenStrikes('10', '9'), 'refuses 10 when the limit the server sent is 9').toMatchObject({ kind: 'refused' });
    expect.soft(chosenStrikes('9', '10'), 'sends 9 when the limit the server sent is 10').toEqual({ kind: 'count', digits: '9' });
    expect.soft(chosenStrikes('15', null), 'sends any whole number while the limit is null: 15').toEqual({ kind: 'count', digits: '15' });
    expect.soft(chosenStrikes('15'), 'sends any whole number while there is no limit: 15').toEqual({ kind: 'count', digits: '15' });
    expect.soft(chosenStrikes('x', '14'), 'says what a box that is not a whole number needs').toEqual({ kind: 'refused', problem: 'Enter a whole number of strikes, or leave the box empty.' });
    expect.soft(chosenStrikes('  ', '14'), 'means the server’s number by an empty box').toEqual({ kind: 'default' });
  });
});
