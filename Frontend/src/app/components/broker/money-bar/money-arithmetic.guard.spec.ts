/** PRD #2560 D12: Python authors every dollar figure and every slice width;
 * the browser does no money arithmetic.
 *
 * This guard finds every source that handles the account-money read — any
 * non-spec `.ts` under `src/app` that names `AccountMoneyView`,
 * `MoneySegment`, `MoneyParts` or `getAccountMoney`, or whose template
 * renders `<app-money-bar>` — together with its component template, and
 * fails if one parses or combines money: `parseFloat`, `parseInt`,
 * `Number(`, `Math.`, `BigInt(`, `toFixed(`, `.reduce(`, or an arithmetic
 * operator beside a `*_usd` field. Display formatting through the `currency`
 * pipe is the one sanctioned conversion, as elsewhere in the broker UI.
 *
 * It discovers its files, so a new money surface (Home, Deploy, the bot page)
 * is guarded the day it reads the view — no list to remember to extend.
 *
 * Falsifiability: the last test feeds each forbidden shape to the same
 * matcher and requires it to be caught.
 */
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

const APP_ROOT = join(__dirname, '..', '..', '..');

const MONEY_READ = /\b(AccountMoneyView|MoneySegment|MoneyParts|getAccountMoney)\b|<app-money-bar\b/;

const FORBIDDEN: readonly (readonly [string, RegExp])[] = [
  ['parseFloat', /\bparseFloat\s*\(/],
  ['parseInt', /\bparseInt\s*\(/],
  ['Number()', /\bNumber\s*\(/],
  ['Math', /\bMath\./],
  ['BigInt', /\bBigInt\s*\(/],
  ['toFixed', /\.toFixed\s*\(/],
  ['reduce', /\.reduce\s*\(/],
  // An operator right before or after a `*_usd` field: `a_usd + b_usd`,
  // `+view.cash_usd`, `total_usd * 2`, `x - y_usd`.
  ['arithmetic on a *_usd field', /[-+*/]\s*[\w.?!()[\]]*_usd\b|\b\w+_usd\b[\w.?!()[\]]*\s*[-+*/](?![/*])/],
];

function offences(source: string): string[] {
  return FORBIDDEN.filter(([, pattern]) => pattern.test(source)).map(([name]) => name);
}

function sourceFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return entry.name === 'api' ? [] : sourceFiles(path);
    return /\.(ts|html)$/.test(entry.name) && !entry.name.endsWith('.spec.ts') ? [path] : [];
  });
}

function moneySources(): string[] {
  const files = sourceFiles(APP_ROOT);
  const present = new Set(files);
  const readers = files.filter((path) => MONEY_READ.test(readFileSync(path, 'utf8')));
  // A component's template and class are one surface: guard both.
  const surfaces = readers.flatMap((path) => [path, path.replace(/\.ts$/, '.html'), path.replace(/\.html$/, '.ts')]);
  return [...new Set(surfaces.filter((path) => present.has(path)))].sort();
}

describe('money arithmetic guard (PRD #2560 D12)', () => {
  it('finds the money surfaces it guards', () => {
    const guarded = moneySources().map((path) => path.slice(APP_ROOT.length + 1));
    expect(guarded).toEqual(expect.arrayContaining([
      join('components', 'broker', 'money-bar', 'money-bar.component.ts'),
      join('components', 'broker', 'money-bar', 'money-bar.component.html'),
      join('components', 'brokers', 'alpaca-desk', 'alpaca-lane-card.component.ts'),
      join('components', 'brokers', 'alpaca-desk', 'alpaca-lane-card.component.html'),
      join('components', 'brokers', 'alpaca-workspace', 'alpaca-account-workspace.component.ts'),
      join('components', 'brokers', 'alpaca-workspace', 'alpaca-account-workspace.component.html'),
    ]));
  });

  it('never parses or combines a dollar string in a money surface', () => {
    const found = moneySources()
      .map((path) => ({ path: path.slice(APP_ROOT.length + 1), offences: offences(readFileSync(path, 'utf8')) }))
      .filter((file) => file.offences.length > 0);
    expect(found).toEqual([]);
  });

  it.each([
    ['const free = parseFloat(view.free_to_deploy_usd);', 'parseFloat'],
    ['const cents = parseInt(segment.amount_usd, 10);', 'parseInt'],
    ['const free = Number(view.free_to_deploy_usd);', 'Number()'],
    ['width = Math.round(share)', 'Math'],
    ['const total = segments.reduce((sum, s) => sum, 0);', 'reduce'],
    ['const free = +view.free_to_deploy_usd;', 'arithmetic on a *_usd field'],
    ['{{ view.cash_usd + view.in_bots_usd }}', 'arithmetic on a *_usd field'],
    ['const left = view.total_usd - spent;', 'arithmetic on a *_usd field'],
  ])('catches %s', (source, name) => {
    expect(offences(source)).toContain(name);
  });

  it('lets the currency pipe format a Python-authored amount', () => {
    expect(offences("{{ segment.amount_usd | currency: 'USD' }}</span>")).toEqual([]);
    expect(offences('readonly free = computed(() => this.money()?.free_to_deploy_usd ?? null);')).toEqual([]);
  });
});
