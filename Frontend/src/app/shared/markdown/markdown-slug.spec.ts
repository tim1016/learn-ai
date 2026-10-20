/**
 * Tests for `markdownSlug`.
 */
import { describe, expect, it } from 'vitest';
import { markdownSlug } from './markdown-slug';

// ── markdownSlug unit tests ───────────────────────────────────────────────────

describe('markdownSlug', () => {
  it.each([
    ['lowercases text', 'Hello World', 'hello-world'],
    ['replaces spaces with hyphens', 'foo bar baz', 'foo-bar-baz'],
    ['strips non-word characters except hyphens', 'Station 1: SIGNAL', 'station-1-signal'],
    ['preserves underscores (word chars)', 'SUBMIT_GATE', 'submit_gate'],
    ['trims leading and trailing hyphens', '  --hello--  ', 'hello'],
    ['collapses multiple consecutive spaces into one hyphen', 'a  b   c', 'a-b-c'],
  ])('%s', (_name, text, expected) => {
    expect(markdownSlug(text)).toBe(expected);
  });
});
