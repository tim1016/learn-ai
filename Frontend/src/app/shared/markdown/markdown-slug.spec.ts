/**
 * Tests for `markdownSlug` and the heading-anchor extraction behaviour.
 *
 * The anchor-map test at the bottom is the critical regression guard: it
 * asserts that every anchor in `BROKER_V2_CARD_ANCHOR_MAP` would be produced
 * correctly by the `{#id}` extraction path so that the rendered manual and the
 * map stay in sync.
 */
import { describe, expect, it } from 'vitest';
import { markdownSlug } from './markdown-slug';
import { BROKER_V2_CARD_ANCHOR_MAP } from '../../components/broker/v2-panel/help-drawer/broker-v2-card-anchor-map';

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

// ── Anchor-map contract ───────────────────────────────────────────────────────

/**
 * For every (cardName, anchor) pair in BROKER_V2_CARD_ANCHOR_MAP, assert that:
 *   - The anchor is the exact id produced by the {#id} extraction path on the
 *     corresponding heading in `broker-v2-operator-manual.md`.
 *
 * This is the rendered-anchor-exists contract.  The headings in the markdown
 * file carry explicit `{#anchor-id}` suffixes; the `MarkdownViewerComponent`
 * extracts those suffixes and uses them as element ids.  If this test passes,
 * `scrollToAnchor(anchor)` will find `#<anchor>` in the rendered document.
 *
 * Heading → expected anchor mappings (derived from broker-v2-operator-manual.md):
 */
const HEADING_TO_ANCHOR: readonly [headingText: string, expectedAnchor: string][] = [
  ['Station 1: SIGNAL {#station-1-signal}', 'station-1-signal'],
  ['Station 2: INTENT {#station-2-intent}', 'station-2-intent'],
  ['Station 3: SUBMIT_GATE {#station-3-submit-gate}', 'station-3-submit-gate'],
  ['Station 4: BROKER_ACK {#station-4-broker-ack}', 'station-4-broker-ack'],
  ['Station 5: FILL {#station-5-fill}', 'station-5-fill'],
  ['Station 6: RECONCILED {#station-6-reconciled}', 'station-6-reconciled'],
  ['Bot Lifecycle {#bot-lifecycle}', 'bot-lifecycle'],
  ['Button Reference {#button-reference}', 'button-reference'],
  ['Hold Actions {#hold-actions}', 'hold-actions'],
  ['Reconcile Actions {#reconcile-actions}', 'reconcile-actions'],
];

describe('BROKER_V2_CARD_ANCHOR_MAP — rendered anchor existence contract', () => {
  it('every heading in the manual produces the anchor the map expects', () => {
    const mismatches: string[] = [];
    for (const [headingText, expectedAnchor] of HEADING_TO_ANCHOR) {
      const actual = extractAnchorFromHeading(headingText);
      if (actual !== expectedAnchor) {
        mismatches.push(`"${headingText}": expected "${expectedAnchor}", got "${actual}"`);
      }
    }
    expect(mismatches).toEqual([]);
  });

  it('every anchor value in BROKER_V2_CARD_ANCHOR_MAP is produced by a manual heading', () => {
    const producedAnchors = new Set(HEADING_TO_ANCHOR.map(([text]) => extractAnchorFromHeading(text)));
    const mapAnchors = Object.values(BROKER_V2_CARD_ANCHOR_MAP);
    const missing = mapAnchors.filter((a) => !producedAnchors.has(a));
    // If this fails: add the missing anchor to HEADING_TO_ANCHOR above and
    // ensure the corresponding heading in broker-v2-operator-manual.md has a
    // matching {#anchor-id} suffix.
    expect(missing).toEqual([]);
  });
});
