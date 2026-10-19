/**
 * Tests for `markdownSlug`.
 */
import { describe, expect, it } from 'vitest';
import { markdownSlug } from './markdown-slug';

// ── markdownSlug unit tests ───────────────────────────────────────────────────

describe('markdownSlug', () => {
  it('lowercases text', () => {
    expect(markdownSlug('Hello World')).toBe('hello-world');
  });

  it('replaces spaces with hyphens', () => {
    expect(markdownSlug('foo bar baz')).toBe('foo-bar-baz');
  });

  it('strips non-word characters except hyphens', () => {
    expect(markdownSlug('Station 1: SIGNAL')).toBe('station-1-signal');
  });

  it('preserves underscores (word chars)', () => {
    expect(markdownSlug('SUBMIT_GATE')).toBe('submit_gate');
  });

  it('trims leading and trailing hyphens', () => {
    expect(markdownSlug('  --hello--  ')).toBe('hello');
  });

  it('collapses multiple consecutive spaces into one hyphen', () => {
    expect(markdownSlug('a  b   c')).toBe('a-b-c');
  });
});
