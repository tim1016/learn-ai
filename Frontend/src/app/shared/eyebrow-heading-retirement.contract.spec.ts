/** #2183 introduced one shared section-heading style — the `eyebrow-heading()`
 * mixin at `styles/_eyebrow-heading.scss` — to replace the app's dozens of
 * hand-rolled "small uppercase label stacked above a heading" pairs (a
 * bespoke element immediately followed by an `h1`–`h6`, two headings for one
 * thing). The bespoke label showed up two ways in this codebase: a
 * component-scoped CSS class literally named `.foo-eyebrow`/`.foo-kicker`,
 * or — less discoverably — a Tailwind utility combo (`uppercase` plus a
 * small/muted-token font-size and color) applied inline with no distinctive
 * class name at all. #2184 swept the whole app to retire both shapes
 * everywhere the label was a genuine duplicate: delete it, move the same
 * look onto the heading that's left. A handful of sites kept both elements —
 * the label carries information the heading text doesn't repeat (see
 * `ALLOWED` below for the specific reason on each) — but even those fold
 * onto one canonical look rather than a second hand-rolled one.
 *
 * This spec is the guard: no template may reintroduce a bespoke
 * eyebrow/kicker-named *or* Tailwind-`uppercase`-styled element sitting
 * directly above a heading — the exact shape the sweep eliminated — without
 * a named, justified entry here.
 *
 * Falsifiability: temporarily reinstate
 * `<p class="foo-eyebrow">Label</p>` (or `<p class="uppercase text-xs">Label</p>`)
 * immediately above an `<h2>` (same indentation, i.e. true siblings) in any
 * in-scope template and this spec fails; remove it, or add a proven
 * `ALLOWED` entry with the same kind of justification the existing entries
 * carry, and it passes again.
 *
 * Known limitation: this is a textual, tag-boundary sibling check, not a DOM
 * parse. It finds opening tags by scanning for `<name ...>` (collapsing any
 * internal newlines, so a class attribute split across lines is still
 * matched) and treats "the next opening tag at the same source-column
 * indent" as "the next sibling element" — reliable for this codebase's
 * consistent per-level template indentation, but it can't see through a
 * reformatted or minified template, and a genuine sibling with a *different*
 * indent (a rare inconsistency, not the norm here) would slip past. It also
 * can't distinguish a *reintroduced* pair from an always-legitimate
 * `ALLOWED` case with different content — the allowlist is by file path, not
 * by the specific text. `uppercase` alone is a broad signal (table headers,
 * chips, and badges use it too) — the sibling-heading requirement is what
 * keeps it from flagging those; a manual audit of every `uppercase` use in
 * the tree at the time this spec was written found exactly four genuine
 * pairs (all fixed) and zero other matches.
 */
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

const APP_ROOT = join(__dirname, '..');

/** Matches a `class="..."` attribute carrying either a bespoke
 * eyebrow/kicker-named class, or the bare Tailwind `uppercase` utility (the
 * inline-styled variant of the same look). */
const EYEBROW_LOOK_PATTERN = /class="[^"]*\b(?:[\w-]*eyebrow[\w-]*|[\w-]*kicker[\w-]*|uppercase)\b[^"]*"/i;

/**
 * Sites where the eyebrow-style label is deliberately kept next to its
 * heading because it carries information the heading text doesn't repeat
 * anywhere else on the page. Verified against the rendered page (not
 * guessed) during the #2184 sweep:
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

interface OpenTag {
  /** The tag's attribute text with internal newlines collapsed to spaces,
   * so a `class="..."` split across lines still matches EYEBROW_LOOK_PATTERN. */
  text: string;
  name: string;
  indent: number;
}

/** Finds every opening/self-closing tag (`<name ...>`) in document order.
 * Closing tags (`</name>`) never match — the regex requires a letter
 * immediately after `<`. */
function openTags(source: string): OpenTag[] {
  const tags: OpenTag[] = [];
  const pattern = /<([a-zA-Z][\w-]*)\b([\s\S]*?)>/g;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(source)) !== null) {
    const lineStart = source.lastIndexOf('\n', match.index) + 1;
    tags.push({
      text: match[0].replace(/\s+/g, ' '),
      name: match[1].toLowerCase(),
      indent: match.index - lineStart,
    });
  }
  return tags;
}

describe('the eyebrow-plus-heading pair is retired, not reintroduced', () => {
  it('no template pairs a bespoke eyebrow/kicker/uppercase label directly above a heading', () => {
    const offenders = new Set<string>();
    for (const path of templateFiles(APP_ROOT)) {
      const relPath = path.slice(APP_ROOT.length + 1);
      const tags = openTags(readFileSync(path, 'utf8'));
      for (let i = 0; i < tags.length - 1; i++) {
        const tag = tags[i];
        if (/^h[1-6]$/.test(tag.name)) continue; // the heading itself now carries the class
        if (!EYEBROW_LOOK_PATTERN.test(tag.text)) continue;

        let j = i + 1;
        while (j < tags.length && tags[j].indent > tag.indent) j++;
        const next = tags[j];
        if (next && next.indent === tag.indent && /^h[1-6]$/.test(next.name)) {
          offenders.add(relPath);
        }
      }
    }
    expect([...offenders].filter((p) => !ALLOWED.has(p))).toEqual([]);
  });
});
