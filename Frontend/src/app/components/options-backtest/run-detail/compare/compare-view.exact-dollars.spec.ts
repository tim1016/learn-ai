import { render, screen, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { reads } from '../../testing/page-actions';
import { RunDetailService } from '../run-detail.service';
import type { RunComparison } from '../run-detail.types';
import { comparison } from '../testing/detail-fixtures';
import { fakeRunDetail } from '../testing/fake-run-detail';
import { CompareViewComponent } from './compare-view.component';

/**
 * Every profit of a comparison is a string the server wrote, and it is shown as written. 9007199254740993.01 is the first
 * whole number-and-cents a double cannot hold.
 * One row for each place the comparison shows a profit, so a binding added later without a row fails the count.
 */
const BIG = '9007199254740993.01';
const SHOWN = '$9,007,199,254,740,993.01';

function answerOf(value: string): RunComparison {
  const answer = comparison();
  return {
    ...answer,
    overlap: {
      ...answer.overlap,
      a: { ...answer.overlap.a, net_profit_usd: value },
      b: { ...answer.overlap.b, net_profit_usd: value },
      sessions: answer.overlap.sessions.map((each) => ({ ...each, a_profit_usd: value, b_profit_usd: value })),
    },
  };
}

async function open(value: string) {
  const view = await render(CompareViewComponent, {
    inputs: { a: 30, b: 33 },
    providers: [{ provide: RunDetailService, useValue: fakeRunDetail(vi, { compare: vi.fn(async () => answerOf(value)) }) }],
  });
  await screen.findByRole('region', { name: 'Each shared session (scrolls sideways)' });
  return view;
}

const cells = (region: string, at: number) =>
  [...within(screen.getByRole('region', { name: region })).getAllByRole('row')].slice(1).map((row) => reads(row.querySelectorAll('td')[at] ?? null));

/** Each place a profit is shown: its name, how to read it off the screen, and how many such places there are. */
const PLACES: readonly (readonly [string, () => string[], number])[] = [
  ['run A’s net profit over the shared sessions', () => cells('Totals over the shared sessions (scrolls sideways)', 0).slice(0, 1), 1],
  ['run B’s net profit over the shared sessions', () => cells('Totals over the shared sessions (scrolls sideways)', 1).slice(0, 1), 1],
  ['run A’s profit in each shared session', () => cells('Each shared session (scrolls sideways)', 1), 3],
  ['run B’s profit in each shared session', () => cells('Each shared session (scrolls sideways)', 3), 3],
];

describe('the profits of a comparison are shown exactly as the server wrote them', () => {
  it.each(PLACES)('%s, a value a float cannot hold', async (_place, read, count) => {
    await open(BIG);

    const shown = read();
    expect(shown.length).toBe(count);
    for (const text of shown) {
      expect(text).toBe(SHOWN);
    }
  });

  it('has a row above for every profit on the screen: no other place shows a dollar amount', async () => {
    const { container } = await open(BIG);

    const amounts = reads(container).match(/-?\$[\d,]+(\.\d+)?/g) ?? [];
    expect(amounts).toEqual(Array(PLACES.reduce((total, [, , count]) => total + count, 0)).fill(SHOWN));
  });
});
