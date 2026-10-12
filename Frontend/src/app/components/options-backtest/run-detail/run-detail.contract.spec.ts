/**
 * Rules the closer look at a run keeps, held by reading its own sources. Each is a rule a reviewer would otherwise have to
 * remember; each is also written in the lint or in a document, and this is the part of it that a lint cannot say.
 *
 * - Time: no code of the folder formats or parses a date by itself. Every instant is shown by the shared timestamp helpers
 *   (`app-timestamp-display`, `timestampDisplay`, `formatTimestampDisplay`, `formatChartAxisTick`).
 * - Money: a price becomes a number in `series-geometry.ts` and nowhere else, only `timeline-plan.ts` asks it to, and no
 *   other file of the folder coerces anything to a number.
 * - The chart library is spoken to by `timeline-chart.component.ts` alone, and its colours are theme tokens.
 *
 * Known limitation: a textual scan. It reads source text, not a syntax tree, so a banned call hidden behind an alias in
 * another file would pass. The lint (`eslint.config.mjs`) is the other half.
 */
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

const FOLDER = __dirname;

function sources(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return sources(path);
    const wanted = /\.(ts|html|scss)$/.test(entry.name) && !entry.name.endsWith('.spec.ts') && !path.includes(`${join(FOLDER, 'testing')}`);
    return wanted ? [path] : [];
  });
}

const FILES = sources(FOLDER).map((path) => ({ name: path.slice(FOLDER.length + 1), text: readFileSync(path, 'utf8') }));
const GEOMETRY = 'series-geometry.ts';

/** The names of the files whose text matches the pattern. */
function filesWith(pattern: RegExp, files = FILES): string[] {
  return files.filter((file) => pattern.test(file.text)).map((file) => file.name).sort();
}

const OWN_DATES = /Intl\.DateTimeFormat|\.toLocale\w*\(|\bDatePipe\b|new Date\(|Date\.parse\(|\.toISOString\(|\.toUTCString\(|\.getTimezoneOffset\(/;
const NUMBER_COERCION = /\bNumber\s*\(|\bparseFloat\s*\(|\bparseInt\s*\(|\bBigInt\s*\(|\bMath\.|\bNumber\.is/;
// A colour written as a literal: in quotes, after a colon, or as six or eight digits that do not follow a word or a bracket (an issue number such as (#3030) is not one).
const HEX_COLOUR = /['"]#[0-9a-fA-F]{3,8}['"]|:\s*#[0-9a-fA-F]{3,8}\b|\b(?:solid|dashed|dotted)\s+#[0-9a-fA-F]{3,8}\b|(?<![\w(&])#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?\b/;

describe('the folder’s own sources are found', () => {
  it('reads the files it means to read, and none of the specs or the test helpers', () => {
    const names = FILES.map((file) => file.name);

    expect(names).toContain('timeline-plan.ts');
    expect(names).toContain(GEOMETRY);
    expect(names).toContain(join('session', 'timeline-chart.component.ts'));
    expect(names).toContain(join('trades', 'trade-table.component.html'));
    expect(names.some((name) => name.endsWith('.spec.ts'))).toBe(false);
    expect(names.some((name) => name.startsWith('testing'))).toBe(false);
  });
});

describe('the patterns that guard the rules still fire', () => {
  it.each([
    ['new Intl.DateTimeFormat("en-US").format(d)'],
    ['value.toLocaleString()'],
    ['row.at | date: "short"; imports: [DatePipe]'],
    ['new Date(text)'],
    ['stamp.toISOString()'],
  ])('finds a date made by hand in %s', (text) => {
    expect(OWN_DATES.test(text)).toBe(true);
  });

  it.each([['Number(price)'], ['parseFloat(price)'], ['parseInt(count, 10)'], ['BigInt(price)'], ['Math.round(x)'], ['Number.isFinite(x)']])('finds a coercion in %s', (text) => {
    expect(NUMBER_COERCION.test(text)).toBe(true);
  });

  it.each([['color: #4d8dff;'], ["const blue = '#4d8dff';"], ['border-top: 1px solid #fff'], ['fill = "#abc"'], ['paint #ff000080 here']])('finds a literal colour in %s', (text) => {
    expect(HEX_COLOUR.test(text)).toBe(true);
  });

  it.each([['color: var(--chart-series-blue);'], ['The run service (#3030) reads them'], ['Issue #3030'], ['<ng-template #absent>'], ['#picker']])('does not mistake %s for one', (text) => {
    expect(HEX_COLOUR.test(text)).toBe(false);
  });
});

describe('time in the closer look', () => {
  it('is never formatted or parsed by a file of the folder: only the shared helpers do that', () => {
    expect(filesWith(OWN_DATES)).toEqual([]);
  });

  it('is shown by a shared helper wherever a file shows one', () => {
    const showers = filesWith(/formatTimestampDisplay|app-timestamp-display|timestampDisplay|formatChartAxisTick/);

    expect(showers).toEqual(
      expect.arrayContaining([
        join('session', 'session-view.component.html'),
        join('session', 'timeline-chart.component.ts'),
        join('trades', 'trade-table.component.html'),
      ]),
    );
  });
});

describe('money in the closer look', () => {
  it('becomes a number in the one geometry file and no other', () => {
    expect(filesWith(NUMBER_COERCION)).toEqual([GEOMETRY]);
  });

  it('is asked of the geometry file by the plan alone, and nothing a template can print comes from it', () => {
    expect(filesWith(/series-geometry|axisPrice/).filter((name) => name !== GEOMETRY)).toEqual(['timeline-plan.ts']);
    expect(filesWith(/axisPrice/, FILES.filter((file) => file.name.endsWith('.html')))).toEqual([]);
  });

  it('says at its top why the geometry file exists and that nothing it makes is printed', () => {
    const header = (FILES.find((file) => file.name === GEOMETRY)?.text ?? '').split('*/')[0].replace(/\s*\n\s*\*\s*/g, ' ');

    expect(header).toContain('THE ONE FILE');
    expect(header).toContain('the only file the money lint');
    expect(header).toContain('nothing this file makes is');
  });
});

describe('the chart in the closer look', () => {
  it('is spoken to by the chart wrapper and by no other file', () => {
    expect(filesWith(/from 'lightweight-charts'/)).toEqual([join('session', 'timeline-chart.component.ts')]);
  });

  it('is made through the application’s own factory and never directly', () => {
    expect(filesWith(/import\s*{[^}]*\bcreateChart\b[^}]*}\s*from\s*'lightweight-charts'/)).toEqual([]);
    expect(filesWith(/createAppChart/)).toEqual([join('session', 'timeline-chart.component.ts')]);
  });

  it('is coloured by theme tokens, with no literal colour anywhere in the folder', () => {
    expect(filesWith(HEX_COLOUR)).toEqual([]);
  });
});
