/**
 * Tests for `markdownSlug` and the heading-anchor extraction behaviour.
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

// ── {#id} extraction logic ───────────────────────────────────────────────────

/**
 * Mirrors the private `addHeadingAnchors` extraction logic from
 * `MarkdownViewerComponent` so we can assert it without reaching into the
 * component's private API.
 *
 * IMPORTANT: keep in sync with the regex in `markdown-viewer.component.ts`.
 */
const EXPLICIT_ANCHOR = /\{#([\w-]+)\}\s*$/;

function extractAnchorFromHeading(rawText: string): string {
  const match = EXPLICIT_ANCHOR.exec(rawText);
  return match ? match[1] : markdownSlug(rawText);
}

describe('explicit {#id} anchor extraction', () => {
  it('extracts the id from a {#id} suffix', () => {
    expect(extractAnchorFromHeading('Station 1: SIGNAL {#station-1-signal}')).toBe(
      'station-1-signal',
    );
  });

  it('falls back to markdownSlug when no {#id} suffix is present', () => {
    expect(extractAnchorFromHeading('Six-Station Pipeline')).toBe('six-station-pipeline');
  });

  it('handles multi-word ids with hyphens', () => {
    expect(extractAnchorFromHeading('Button Reference {#button-reference}')).toBe(
      'button-reference',
    );
  });

  it('handles trailing whitespace before the suffix', () => {
    expect(extractAnchorFromHeading('Hold Actions  {#hold-actions}  ')).toBe('hold-actions');
  });
});
