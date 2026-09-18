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
 *
 * All three scans — the class-read scan, the template-side declaration
 * lookup, and the discovery cross-check — run on `.ts` source with comments
 * blanked out by `stripComments` first, and share that one implementation.
 * A prose comment naming `resource()`, or a trailing `//` on an otherwise
 * correctly guarded declaration line, is real text that both regexes above
 * see; without stripping, one falsely names a guarded file as an offender
 * and the other falsely reports a discovery mismatch. `stripComments` uses
 * the TypeScript parser rather than a hand-rolled regex or a bare
 * `ts.createScanner` pass — see its doc comment for why a bare scanner was
 * tried and rejected. Getting this wrong in the other direction (stripping
 * too much) is the worse failure: a `//` inside a string, template, or
 * regex literal must not start a comment, or a real unguarded read on that
 * line goes uninspected and the guard passes silently. The fixture tests
 * below pin both directions.
 */
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import * as ts from 'typescript';
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

/**
 * Blanks every comment in TypeScript source with spaces, keeping newlines,
 * line numbers, statement-boundary offsets (`;`/`{`/`}`), and every
 * non-comment character byte-identical to the input. All three scans in
 * this file run on this output, not the raw source — see the file header.
 *
 * Backed by the real TypeScript parser (vendored for the Angular compiler —
 * `Frontend/node_modules/typescript`, so this is not a new dependency), not
 * a hand-rolled regex or a bare `ts.createScanner` pass. A bare scanner was
 * tried first: fed a genuine regex literal like `/a\/\/b/`, it cannot tell
 * "`/` starts a regex" from "`/` starts a division" without grammar
 * context, mis-scans the back half as a same-line `//` comment, and
 * silently eats real code — the exact silent-strips-too-much failure this
 * function exists to prevent (a `//` inside a string, template, or regex
 * literal must never start a comment). The parser resolves that ambiguity;
 * walking its concrete leaf tokens and reading each one's actual comment
 * trivia via `ts.getLeadingCommentRanges`/`ts.getTrailingCommentRanges` is
 * the rest of the work. This does not change what the guard checks: the
 * `.value()`/`.hasValue()` matching below is still plain text on the result.
 */
function stripComments(source: string): string {
  const sourceFile = ts.createSourceFile(
    'resource-guard-scan.ts',
    source,
    ts.ScriptTarget.Latest,
    /* setParentNodes */ true,
    ts.ScriptKind.TS,
  );
  const leaves: ts.Node[] = [];
  const collectLeaves = (node: ts.Node): void => {
    const children = node.getChildren(sourceFile);
    if (children.length === 0) {
      leaves.push(node);
      return;
    }
    children.forEach(collectLeaves);
  };
  collectLeaves(sourceFile);

  const chars = source.split('');
  const blank = (pos: number, end: number): void => {
    for (let i = pos; i < end; i++) if (chars[i] !== '\n') chars[i] = ' ';
  };
  for (const token of leaves) {
    for (const range of ts.getTrailingCommentRanges(source, token.getEnd()) ?? []) blank(range.pos, range.end);
    for (const range of ts.getLeadingCommentRanges(source, token.getFullStart()) ?? []) blank(range.pos, range.end);
  }
  return chars.join('');
}

/** Blanks `<!-- ... -->` template comments the same way `stripComments`
 * blanks `.ts` ones, so an HTML comment naming a resource read can't
 * false-alarm the template scan. A regex is safe here — unlike the `.ts`
 * case above, Angular templates have no string/regex-literal construct that
 * could contain a stray `-->`. */
function stripHtmlComments(source: string): string {
  return source.replace(/<!--[\s\S]*?-->/g, (comment) => comment.replace(/[^\n]/g, ' '));
}

function declaredResources(classSource: string): string[] {
  return [...classSource.matchAll(RESOURCE_DECLARATION)].map((match) => match[1]);
}

/** One entry per class-source `.value()` read of a declared resource that no
 * `.hasValue()` precedes within its statement. Comments are stripped first so
 * prose naming a read is not mistaken for one. */
function unguardedClassReads(classSource: string): string[] {
  const code = stripComments(classSource);
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
      const lines = stripHtmlComments(readFileSync(path, 'utf8')).split('\n');
      const resources = declaredResources(stripComments(readFileSync(path.replace(/\.html$/, '.ts'), 'utf8')));
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

  it('a trailing comment naming a guarded read does not trip the class scan', () => {
    // Reproduces a real false positive: a trailing `//` comment on the
    // rxResource(...) declaration line itself, naming the identifier's
    // already-guarded .value() read below. Before stripComments handled
    // trailing comments, this line's raw text (comment included) matched
    // the "unguarded read" regex and flagged a file doing everything right.
    const source = `
      private readonly supportedIndicatorResource = rxResource({ // seeds supportedIndicatorResource.value() below
        params: () => 'chart-indicator-catalog',
        stream: () => this.indicatorService.supportedIndicators(),
      });
      protected readonly indicatorCategories = computed(() => {
        const supported = new Set(
          this.supportedIndicatorResource.hasValue()
            ? this.supportedIndicatorResource.value()?.names ?? []
            : [],
        );
      });`;
    expect(unguardedClassReads(source)).toEqual([]);
  });

  it('a `//` inside a string literal is not mistaken for a comment — the silent-pass direction', () => {
    // The dangerous direction: over-stripping. If a naive strip started a
    // comment at the first "//" on a line, the "https://" URL below would
    // swallow the real, unguarded histChart.value() read that follows it on
    // the same line, and the guard would pass having checked nothing.
    const source = `
      private readonly histChart = resource({
        params: () => 'https://example.test',
        loader: () => this.service.fetch(),
      });
      protected readonly label = computed(() => {
        const url = 'https://example.test'; return this.histChart.value();
      });`;
    expect(unguardedClassReads(source)).toEqual(['histChart']);
  });

  it('a block comment spanning lines naming x.value() does not trip the class scan', () => {
    const source = `
      private readonly tape = resource({
        params: () => this.symbol(),
        loader: () => this.service.fetch(),
      });
      /**
       * Do not call tape.value() directly — always check tape.hasValue()
       * first, the way the rest of this file does.
       */
      protected readonly summary = computed(() => (
        this.tape.hasValue() ? this.tape.value() : null
      ));`;
    expect(unguardedClassReads(source)).toEqual([]);
  });

  it('a prose comment naming resource() does not trip declaration discovery', () => {
    // Reproduces a real false positive: a comment describing the API used,
    // not a declaration, containing the literal text "resource(". Before
    // both counts ran on stripComments output, the loose regex counted this
    // comment as an extra call site the strict regex never claimed to find.
    const source = `
      // Uses Angular's resource() API for the previous-run lookup.
      private readonly previousRun = resource({
        params: () => this.runId(),
        loader: () => this.service.fetch(),
      });`;
    const code = stripComments(source);
    expect(declaredResources(code).length).toEqual(
      [...code.matchAll(LOOSE_RESOURCE_OCCURRENCE)].length,
    );
  });

  it('declaration discovery finds every resource()/rxResource()/httpResource() call site, or the two scans above are checking nothing', () => {
    const mismatches: string[] = [];
    for (const path of sourceFiles(V2_PANEL_ROOT, '.ts')) {
      const relPath = path.slice(V2_PANEL_ROOT.length + 1);
      const code = stripComments(readFileSync(path, 'utf8'));
      const strictCount = declaredResources(code).length;
      const looseCount = [...code.matchAll(LOOSE_RESOURCE_OCCURRENCE)].length;
      if (strictCount !== looseCount) {
        mismatches.push(`${relPath}: RESOURCE_DECLARATION found ${strictCount}, but ${looseCount} call site(s) exist`);
      }
    }
    expect(mismatches).toEqual([]);
  });
});
