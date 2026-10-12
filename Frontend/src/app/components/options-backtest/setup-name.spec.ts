import { describe, expect, it } from 'vitest';

import { isValidSetupName } from './setup-name';

describe('a setup name', () => {
  it.each(['a', 'SPY premium momentum, one re-entry', 'x_y-z.(1),2', 'a b', '0', 'a'.repeat(80)])('is taken: "%s"', (name) => {
    expect(isValidSetupName(name)).toBe(true);
  });

  it.each([
    ['', 'empty'],
    [' a', 'a space first'],
    ['a ', 'a space last'],
    [' ', 'only a space'],
    ['a'.repeat(81), 'longer than 80'],
    ['a/b', 'a slash'],
    ['a\\b', 'a backslash'],
    ['a?b', 'a question mark'],
    ['a#b', 'a hash'],
    ['a%b', 'a percent sign'],
    ['a;b', 'a semicolon'],
    ['é', 'a letter outside ASCII'],
    ['a\nb', 'a new line'],
    ['a\n', 'a trailing new line'],
  ])('is refused: "%s" (%s)', (name) => {
    expect(isValidSetupName(name)).toBe(false);
  });
});
