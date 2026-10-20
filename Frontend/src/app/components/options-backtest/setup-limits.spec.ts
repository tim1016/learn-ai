import { describe, expect, it } from 'vitest';

import type { SetupLimits } from './options-backtest.types';
import { legsFull, nameProblem } from './setup-limits';
import { SERVED_LIMITS } from './testing/fixtures';

const SENTENCE = SERVED_LIMITS.setup_name.sentence;

describe('a setup name, judged by the rule the server serves', () => {
  it.each(['a', 'SPY premium momentum, one re-entry', 'x_y-z.(1),2', 'a b', '0', 'a'.repeat(80)])('is taken: "%s"', (name) => {
    expect(nameProblem(name, SERVED_LIMITS)).toBeNull();
  });

  it.each([
    ['a space first', ' a'],
    ['a space last', 'a '],
    ['only a space', ' '],
    ['longer than 80', 'a'.repeat(81)],
    ['a slash', 'a/b'],
    ['a backslash', 'a\\b'],
    ['a question mark', 'a?b'],
    ['a hash', 'a#b'],
    ['a percent sign', 'a%b'],
    ['a semicolon', 'a;b'],
    ['a letter outside ASCII', 'é'],
    ['a new line', 'a\nb'],
    ['a trailing new line', 'a\n'],
    ['a dot alone', '.'],
    ['two dots', '..'],
    ['a dot first', '.hidden'],
    ['a dash first', '-dash'],
    ['an underscore first', '_under'],
    ['a bracket first', '(paren'],
  ])('is refused with the server’s own sentence: %s', (_why, name) => {
    expect(nameProblem(name, SERVED_LIMITS)).toBe(SENTENCE);
  });

  it.each<[string, string, SetupLimits | null]>([
    ['says nothing about an empty name, which Save asks for in its own words', '', SERVED_LIMITS],
    ['leaves the judging to the server while the limits are not known', 'a/b', null],
    ['leaves the judging to the server when this browser cannot read the pattern it was given', 'a/b', { ...SERVED_LIMITS, setup_name: { pattern: '(unclosed', sentence: 'x' } }],
  ])('%s', (_why, name, limits) => {
    expect(nameProblem(name, limits)).toBeNull();
  });

  it('follows the pattern and the sentence that are served, not a rule of its own', () => {
    const lowerCaseOnly: SetupLimits = { ...SERVED_LIMITS, setup_name: { pattern: '^[a-z]+$', sentence: 'Lower-case letters only.' } };

    expect(nameProblem('Mine', lowerCaseOnly)).toBe('Lower-case letters only.');
    expect(nameProblem('mine', lowerCaseOnly)).toBeNull();
  });
});

describe('when the legs are full', () => {
  it('is when the setup holds as many as the server allows, and never while the limit is not known', () => {
    const limits: SetupLimits = { ...SERVED_LIMITS, max_legs: 3 };

    expect.soft(legsFull(2, limits), 'is not full below the limit the server allows').toBe(false);
    expect.soft(legsFull(3, limits), 'is full when the setup holds as many as the server allows').toBe(true);
    expect.soft(legsFull(4, limits), 'is full above the limit the server allows').toBe(true);
    expect.soft(legsFull(500, null), 'is never full while the limit is not known').toBe(false);
  });
});
