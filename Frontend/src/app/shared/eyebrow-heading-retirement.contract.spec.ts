/** #2183 introduced one shared section-heading style — the `eyebrow-heading()`
 * mixin at `styles/_eyebrow-heading.scss` — to replace the app's dozens of
 * hand-rolled "small uppercase label stacked above a heading" pairs (a
 * bespoke `.foo-eyebrow`/`.foo-kicker` element immediately followed by an
 * `h1`–`h6`, two headings for one thing). #2184 swept the whole app to
 * retire that pair everywhere it was a genuine duplicate: delete the label,
 * move the mixin onto the heading that's left. A handful of sites kept both
 * elements — the label carries information the heading text doesn't repeat
 * (see `ALLOWED` below for the specific reason on each) — but even those
 * fold the label's own CSS onto the shared mixin instead of a hand-rolled
 * duplicate.
 *
 * This spec is the guard: no template may reintroduce a bespoke
 * eyebrow/kicker-named element sitting directly above a heading — the exact
 * shape the sweep eliminated — without a named, justified entry here.
 *
 * Falsifiability: temporarily reinstate
 * `<p class="foo-eyebrow">Label</p>` immediately above an `<h2>` (same
 * indentation, i.e. true siblings) in any in-scope template and this spec
 * fails; remove it, or add a proven `ALLOWED` entry with the same kind of
 * justification the two existing entries carry, and it passes again.
 *
 * Known limitation: this is a textual, indentation-based sibling check, not
 * a DOM parse. "The next non-blank line at the same leading-whitespace
 * width as the eyebrow-classed line" stands in for "the next sibling
 * element" — reliable for this codebase's consistent per-level template
 * indentation, but it can't see through a reformatted or minified template,
 * and it only looks at the single next non-blank line, so an eyebrow
 * separated from its heading by another sibling in between would slip past
 * (no such case exists in the tree today). It also can't distinguish a
 * *reintroduced* pair from one that was always a legitimate `ALLOWED` case
 * with different content — the allowlist is by file path, not by the
 * specific text.
 */
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

const APP_ROOT = join(__dirname, '..');

const EYEBROW_CLASS_PATTERN = /class="[^"]*\b\S*(?:eyebrow|kicker)\S*\b[^"]*"/i;
const HEADING_OPEN_PATTERN = /<h[1-6][\s>]/;

/**
 * Sites where the eyebrow-style label is deliberately kept next to its
 * heading because it carries information the heading text doesn't repeat
 * anywhere else on the page. Both were verified against the rendered page
 * (not guessed) during the #2184 sweep:
 *
 *  - metric-reference-entry: the `{{ item.metric_id }}` eyebrow (e.g.
 *    "sharpe_ratio") is the machine identifier for a metric shown in a
 *    developer-reference component; grepped for other consumers of
 *    `metric_id` in this feature and found only internal query-param
 *    routing, never a second visible surface.
 *  - walk-forward-study-result: the "Verdict · based on N of M folds"
 *    eyebrow's fold-completeness disclosure is not repeated in the facts
 *    list rendered below it (retention / Sharpe / trade count / winner
 *    changes, but not fold completeness).
 *
 * Both still fold their own CSS onto the shared `eyebrow-heading()` mixin
 * (see their .scss files) — only the HTML pairing survives, not a bespoke
 * hand-rolled look.
 */
const ALLOWED = new Set<string>([
  join('components', 'strategy-lab', 'analytical-manual', 'metric-reference-entry.component.html'),
  join('components', 'walk-forward-study', 'walk-forward-study-result.component.html'),
]);

/** Templates: real `.html` files, plus non-spec `.ts` files, which can carry
 * an inline `template:` (this is how the #2184 sweep found
 * `markdown-drawer-host.component.ts` — a component with no `.html`/`.scss`
 * file at all, invisible to a `*.html`/`*.scss` grep). */
function templateFiles(dir: string): string[] {
  const found: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) {
      found.push(...templateFiles(path));
      continue;
    }
    const isHtml = entry.name.endsWith('.html');
    const isNonSpecTs = entry.name.endsWith('.ts') && !entry.name.endsWith('.spec.ts');
    if (isHtml || isNonSpecTs) found.push(path);
  }
  return found;
}

function indentWidth(line: string): number {
  return line.length - line.trimStart().length;
}

describe('the eyebrow-plus-heading pair is retired, not reintroduced', () => {
  it('no template pairs a bespoke eyebrow/kicker label directly above a heading', () => {
    const offenders = new Set<string>();
    for (const path of templateFiles(APP_ROOT)) {
      const relPath = path.slice(APP_ROOT.length + 1);
      const lines = readFileSync(path, 'utf8').split('\n');
      for (let i = 0; i < lines.length; i++) {
        const line = lines[i];
        if (!EYEBROW_CLASS_PATTERN.test(line)) continue;
        if (HEADING_OPEN_PATTERN.test(line)) continue; // the heading itself now carries the class

        const width = indentWidth(line);
        for (let j = i + 1; j < lines.length; j++) {
          const next = lines[j];
          if (next.trim() === '') continue;
          if (indentWidth(next) === width && HEADING_OPEN_PATTERN.test(next)) {
            offenders.add(relPath);
          }
          break;
        }
      }
    }
    expect([...offenders].filter((p) => !ALLOWED.has(p))).toEqual([]);
  });
});
