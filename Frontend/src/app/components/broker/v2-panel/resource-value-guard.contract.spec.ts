/** #2202 (PRD #2201 §8 FR-001): an Angular `resource()`'s `.value()` throws
 * `ResourceValueError` while the resource is in its error state. Reading it
 * unguarded in a template — `histChart.value() ?? null`,
 * `journalPage.value() ?? null` — or in a class `computed()` the template
 * reads — `supportedIndicatorResource.value()?.names` — throws during
 * rendering, and Angular abandons the whole change-detection pass, freezing
 * every unrelated control on the page (#2201 §2.1). The `??`/`?.` never runs:
 * the call itself throws. This spec is the guard: no Broker V2 template or
 * component class may reintroduce an unguarded resource-value read without a
 * named, justified ALLOWED entry.
 *
 * Falsifiability: run against the pre-#2202 sources —
 * `panel-shell/bot-panel-shell.component.html`'s `histChart.value() ?? null`,
 * `operator-lens/operator-lens.component.html`'s
 * `journalPage.value() ?? null`, and
 * `dual-pane-chart/dual-pane-chart.component.ts`'s
 * `this.supportedIndicatorResource.value()?.names` — and this spec fails,
 * listing each as `<relative-path>::<identifier>`. The fix (`hasValue() ?
 * value() : null`, the pattern `bot-triage-detail.component.html`'s `journal`
 * binding already uses) is what makes it pass again. The class scan's own
 * fixture case below pins that it still fires.
 *
 * Scope: identifiers each component declares as a resource
 * (`readonly <name> = resource(` / `rxResource(` / `httpResource(`), not
 * every `.value()` call. Signals, form controls, and plain objects
 * legitimately expose a `.value()` member; scanning for those too would
 * false-positive on unrelated work and get this spec disabled the first time
 * it blocks an unrelated PR (PRD #2201 §8 FR-001). A template is checked
 * against the resources its sibling `.ts` declares; a class against its own.
 * Discovering them rather than listing them means a new resource is covered
 * the moment it is declared.
 *
 * Known limitation: a textual check, not an Angular-expression or TypeScript
 * parse (the same class of limitation `lane-fence-freeze.contract.spec.ts`
 * and `eyebrow-heading-retirement.contract.spec.ts` document for their own
 * scans). A template read passes when `<identifier>.hasValue()` is on the
 * SAME source line — true for every binding in this directory today, all
 * one-line template expressions. A class read passes when
 * `<identifier>.hasValue()` appears earlier in the same statement (the text
 * since the last `;`, `{` or `}`), since class ternaries wrap across lines.
 * Neither can prove the two calls share one ternary branch versus merely
 * co-occurring, and a guard by early return (`if (!x.hasValue()) return;`)
 * or by reachability (a template `@else` after a failure branch, a stream
 * that `catchError`s into a value) is invisible to it — inline the guard
 * rather than relying on one.
 *
 * A check that cannot fire is worse than no check: auto-discovery means a
 * declaration shape `RESOURCE_DECLARATION` doesn't match (no `readonly`, a
 * type annotation between the name and `=`, a new factory name) finds zero
 * identifiers for that class and the guard tests pass green having checked
 * nothing. The "declaration discovery" test below closes that hole by
 * counting call sites with a second, deliberately loose regex that only
 * requires the factory name followed by `(`/`<` — no `readonly`, no
 * assignment shape — and failing if the strict and loose counts disagree
 * anywhere under this directory.
 */
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

const V2_PANEL_ROOT = __dirname;

const RESOURCE_DECLARATION = /\breadonly\s+(\w+)\s*=\s*(?:rxResource|httpResource|resource)\b/g;

/** Every `resource()`/`rxResource()`/`httpResource()` call site, regardless
 * of declaration shape. Used only to cross-check `RESOURCE_DECLARATION`'s
 * discovery count per file — see "declaration discovery" test below. */
const LOOSE_RESOURCE_OCCURRENCE = /(?:rxResource|httpResource|resource)\s*[<(]/g;

/**
 * `<relative-path-from-this-dir>::<identifier>` entries deliberately exempt
 * from the guard.
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

function sourceFiles(dir: string, extension: '.html' | '.ts'): string[] {
  const found: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) {
      found.push(...sourceFiles(path, extension));
      continue;
    }
    if (entry.name.endsWith(extension) && !entry.name.endsWith('.spec.ts')) found.push(path);
  }
  return found;
}

function declaredResources(classSource: string): string[] {
  return [...classSource.matchAll(RESOURCE_DECLARATION)].map((match) => match[1]);
}

/** One entry per class-source `.value()` read of a declared resource that no
 * `.hasValue()` precedes within its statement. Comments are stripped first so
 * prose naming a read is not mistaken for one. */
function unguardedClassReads(classSource: string): string[] {
  const code = classSource.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
  const offenders: string[] = [];
  for (const identifier of declaredResources(code)) {
    const guardPattern = new RegExp(`\\b${identifier}\\.hasValue\\(\\)`);
    for (const read of code.matchAll(new RegExp(`\\b${identifier}\\.value\\(\\)`, 'g'))) {
      const before = code.slice(0, read.index);
      const statementStart =
        Math.max(before.lastIndexOf(';'), before.lastIndexOf('{'), before.lastIndexOf('}')) + 1;
      if (!guardPattern.test(before.slice(statementStart))) offenders.push(identifier);
    }
  }
  return offenders;
}

describe('Broker V2 never reads an unguarded resource value', () => {
  it('every template resource .value() read is guarded by .hasValue() on the same line, or is a named, proven exception', () => {
    const offenders: string[] = [];
    for (const path of sourceFiles(V2_PANEL_ROOT, '.html')) {
      const relPath = path.slice(V2_PANEL_ROOT.length + 1);
      const lines = readFileSync(path, 'utf8').split('\n');
      const resources = declaredResources(readFileSync(path.replace(/\.html$/, '.ts'), 'utf8'));
      for (const identifier of resources) {
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

  it('every class resource .value() read is guarded by .hasValue() within its statement, or is a named, proven exception', () => {
    const offenders: string[] = [];
    for (const path of sourceFiles(V2_PANEL_ROOT, '.ts')) {
      const relPath = path.slice(V2_PANEL_ROOT.length + 1);
      for (const identifier of unguardedClassReads(readFileSync(path, 'utf8'))) {
        const key = `${relPath}::${identifier}`;
        if (!ALLOWED.has(key)) offenders.push(key);
      }
    }
    expect(offenders).toEqual([]);
  });

  it('the class scan flags the pre-#2202 indicator-catalog read and accepts its guarded fix', () => {
    const declaration = `
      private readonly supportedIndicatorResource = rxResource({
        params: () => 'chart-indicator-catalog',
        stream: () => this.indicatorService.supportedIndicators(),
      });`;

    expect(unguardedClassReads(`${declaration}
      protected readonly indicatorCategories = computed(() => {
        const supported = new Set(this.supportedIndicatorResource.value()?.names ?? []);
      });`)).toEqual(['supportedIndicatorResource']);
    expect(unguardedClassReads(`${declaration}
      protected readonly indicatorCategories = computed(() => {
        const supported = new Set(
          this.supportedIndicatorResource.hasValue()
            ? this.supportedIndicatorResource.value().names
            : [],
        );
      });`)).toEqual([]);
  });

  it('declaration discovery finds every resource()/rxResource()/httpResource() call site, or the two scans above are checking nothing', () => {
    const mismatches: string[] = [];
    for (const path of sourceFiles(V2_PANEL_ROOT, '.ts')) {
      const relPath = path.slice(V2_PANEL_ROOT.length + 1);
      const source = readFileSync(path, 'utf8');
      const strictCount = declaredResources(source).length;
      const looseCount = [...source.matchAll(LOOSE_RESOURCE_OCCURRENCE)].length;
      if (strictCount !== looseCount) {
        mismatches.push(`${relPath}: RESOURCE_DECLARATION found ${strictCount}, but ${looseCount} call site(s) exist`);
      }
    }
    expect(mismatches).toEqual([]);
  });
});
