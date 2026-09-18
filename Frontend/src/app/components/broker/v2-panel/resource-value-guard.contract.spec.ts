/** #2202 (PRD #2201 §8 FR-001): an Angular `resource()`'s `.value()` throws
 * `ResourceValueError` while the resource is in its error state. Reading it
 * unguarded in a template — `histChart.value() ?? null`,
 * `journalPage.value() ?? null` — throws during rendering, and Angular
 * abandons the whole change-detection pass, freezing every unrelated control
 * on the page (#2201 §2.1). This spec is the guard: no Broker V2 template may
 * reintroduce an unguarded resource-value read without a named, justified
 * ALLOWED entry.
 *
 * Falsifiability: run against the pre-#2202 sources —
 * `panel-shell/bot-panel-shell.component.html`'s `histChart.value() ?? null`
 * and `operator-lens/operator-lens.component.html`'s
 * `journalPage.value() ?? null` — and this spec fails, listing both as
 * `<relative-path>::<identifier>`. The fix (`hasValue() ? value() : null`,
 * the pattern `bot-triage-detail.component.html`'s `journal` binding already
 * uses) is what makes it pass again.
 *
 * Scope: a declared list of resource identifiers, not every `.value()` call.
 * Signals, form controls, and plain objects legitimately expose a `.value()`
 * member; scanning for those too would false-positive on unrelated work and
 * get this spec disabled the first time it blocks an unrelated PR (PRD #2201
 * §8 FR-001). Adding a resource to a Broker V2 component's template means
 * adding its identifier to `RESOURCE_IDENTIFIERS` below.
 *
 * Known limitation: a textual, per-line check, not an Angular-expression
 * parse (the same class of limitation `lane-fence-freeze.contract.spec.ts`
 * and `eyebrow-heading-retirement.contract.spec.ts` document for their own
 * scans). It requires `<identifier>.value()` and `<identifier>.hasValue()`
 * to appear on the SAME source line — true for every binding in this
 * directory today, all one-line template expressions — and cannot prove the
 * two calls share one ternary branch versus merely co-occurring on a long
 * line. It also cannot see a resource read from a `computed()` in the
 * component class and passed to the template as an already-guarded plain
 * value (e.g. `bot-triage-detail`'s `tapeFailed`/`tapeBars`) — those never
 * appear in `RESOURCE_IDENTIFIERS` because their template never calls
 * `.value()`/`.hasValue()` directly, so they need no entry at all.
 */
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

const V2_PANEL_ROOT = __dirname;

/** Every resource identifier known to be read via `.value()` directly in a
 * Broker V2 template. Not every `resource()`/`rxResource()` in this
 * directory needs an entry — only ones a template reads with `.value()`;
 * see the "Known limitation" note above. */
const RESOURCE_IDENTIFIERS: readonly string[] = ['profile', 'histChart', 'journalPage', 'journal'];

/**
 * `<relative-path-from-this-dir>::<identifier>` entries deliberately exempt
 * from the same-line guard.
 *
 * - `panel-shell/bot-panel-shell.component.html::profile` — `@let profileData
 *   = profile.value()!;` is not guarded inline, but it is guarded: the whole
 *   block it lives in is `@if (isLoaded())`, and
 *   `isLoaded = computed(() => this.panel() !== null &&
 *   this.profile.hasValue())` in `bot-panel-shell.component.ts`. Every line
 *   inside that block runs only once `profile.hasValue()` is already true.
 */
const ALLOWED = new Set<string>([
  `${join('panel-shell', 'bot-panel-shell.component.html')}::profile`,
]);

function htmlFiles(dir: string): string[] {
  const found: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) {
      found.push(...htmlFiles(path));
      continue;
    }
    if (entry.name.endsWith('.html')) found.push(path);
  }
  return found;
}

describe('Broker V2 templates never read an unguarded resource value', () => {
  it('every declared resource .value() read is guarded by .hasValue() on the same line, or is a named, proven exception', () => {
    const offenders: string[] = [];
    for (const path of htmlFiles(V2_PANEL_ROOT)) {
      const relPath = path.slice(V2_PANEL_ROOT.length + 1);
      const lines = readFileSync(path, 'utf8').split('\n');
      for (const identifier of RESOURCE_IDENTIFIERS) {
        const valuePattern = new RegExp(`\\b${identifier}\\.value\\(\\)`);
        const guardPattern = new RegExp(`\\b${identifier}\\.hasValue\\(\\)`);
        for (const line of lines) {
          if (!valuePattern.test(line) || guardPattern.test(line)) continue;
          const key = `${relPath}::${identifier}`;
          if (!ALLOWED.has(key)) offenders.push(key);
        }
      }
    }
    expect(offenders).toEqual([]);
  });
});
