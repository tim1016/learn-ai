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

  it('says nothing about an empty name, which Save asks for in its own words', () => {
    expect(nameProblem('', SERVED_LIMITS)).toBeNull();
  });

  it('follows the pattern and the sentence that are served, not a rule of its own', () => {
    const lowerCaseOnly: SetupLimits = { ...SERVED_LIMITS, setup_name: { pattern: '^[a-z]+$', sentence: 'Lower-case letters only.' } };

    expect(nameProblem('Mine', lowerCaseOnly)).toBe('Lower-case letters only.');
    expect(nameProblem('mine', lowerCaseOnly)).toBeNull();
  });

  it('leaves the judging to the server while the limits are not known', () => {
    expect(nameProblem('a/b', null)).toBeNull();
  });

  it('leaves the judging to the server when this browser cannot read the pattern it was given', () => {
    const unreadable: SetupLimits = { ...SERVED_LIMITS, setup_name: { pattern: '(unclosed', sentence: 'x' } };

    expect(nameProblem('a/b', unreadable)).toBeNull();
  });
});

describe('when the legs are full', () => {
  it('is when the setup holds as many as the server allows', () => {
    const limits: SetupLimits = { ...SERVED_LIMITS, max_legs: 3 };

    expect(legsFull(2, limits)).toBe(false);
    expect(legsFull(3, limits)).toBe(true);
    expect(legsFull(4, limits)).toBe(true);
  });

  it('is never, while the limit is not known', () => {
    expect(legsFull(500, null)).toBe(false);
  });
});
