/** #2858: every date on the site is picked with the shared date controls
 * (`app-date-field`, `app-date-range-picker`). The browser's own date field
 * applied each keystroke of a half-typed year as a whole date, took any day
 * whatever the data held, and looked different in every browser; seven pages
 * had grown seven ways of using it. This spec is the guard: no template may
 * bring a raw `<input type="date">` back.
 *
 * Falsifiability: run against the sources before #2858 and it lists
 * `golden-search-time-windows.component.html`, `grid-search-spec-editor`,
 * `data-lab`, the two Data Lake forms, `bot-end-editor` and
 * `time-window-card`. The fixture case below pins that the scan still fires.
 *
 * Scope: `<input … type="date" …>` in any `.html` or non-spec `.ts` under
 * `src/app`. PrimeNG's own `<p-column-filter type="date">` is not an input
 * and opens the same calendar the shared controls use.
 *
 * Known limitation: a textual check, not a template parse. It reads up to
 * the tag's first `>`, so a bound expression containing `>` before the
 * `type` attribute hides the field; and a comment that quotes the tag fails
 * too, which is deliberate: describe a date field in words.
 */
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

const APP_ROOT = join(__dirname, '..', '..');
const RAW_DATE_FIELD = /<input\b[^>]*\btype\s*=\s*["']date["']/g;

/** The 1-based lines on which `source` opens a raw date field. */
function rawDateFieldLines(source: string): number[] {
  return [...source.matchAll(RAW_DATE_FIELD)].map((match) => source.slice(0, match.index).split('\n').length);
}

function sourceFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return sourceFiles(path);
    const scanned = entry.name.endsWith('.html') || (entry.name.endsWith('.ts') && !entry.name.endsWith('.spec.ts'));
    return scanned ? [path] : [];
  });
}

describe('raw date fields (#2858)', () => {
  it('finds a raw date field wherever its type attribute sits in the tag', () => {
    expect(rawDateFieldLines('<label>\n  <input\n    #from\n    type="date"\n    [value]="from()" />\n</label>')).toEqual([2]);
    expect(rawDateFieldLines(`<input type='date'>`)).toEqual([1]);
    expect(rawDateFieldLines('<input type="text" />\n<p-column-filter type="date" field="recordedAtMs" />\n<app-date-field />')).toEqual([]);
  });

  it('no page picks a date with a raw date field: use app-date-field or app-date-range-picker', () => {
    const offenders = sourceFiles(APP_ROOT).flatMap((path) =>
      rawDateFieldLines(readFileSync(path, 'utf8')).map((line) => `${path.slice(APP_ROOT.length + 1)}:${line}`),
    );

    expect(offenders).toEqual([]);
  });
});
