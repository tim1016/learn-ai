import { screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { reads } from '../testing/page-actions';
import { renderHistoryTab } from '../testing/render-history';
import { fakeService } from '../testing/render-page';
import { runItem } from '../testing/run-fixtures';

/**
 * A run's headline dollars in the list are the strings the server wrote, shown as written. 9007199254740993 is the
 * first whole number a double cannot hold, and Angular's `currency` pipe would show it as ...994.00 (see the same
 * table for the result in run/run-result.exact-dollars.spec.ts): one row for each place the list shows a dollar.
 */
const BIG = '9007199254740993.01';
const SHOWN = '$9,007,199,254,740,993.01';

const HEADLINE = { sessions_tested: 10, sessions_traded: 8, trades: 12, trades_won: 7, net_profit_usd: BIG, ending_cash_usd: BIG };

async function renderList() {
  const service = fakeService(vi, { runs: vi.fn(async () => [runItem({ id: 30, job_id: 'job-30', status: 'complete', headline: HEADLINE, finished_at_ms: 1 })]) });
  await renderHistoryTab(vi, { service });
  await screen.findByRole('button', { name: 'Open run 30' });
}

const resultCell = () => (screen.getByRole('button', { name: 'Open run 30' }).closest('tr') as HTMLElement).querySelectorAll('td')[4] as HTMLElement;

describe('the dollars of a run in the list are shown exactly as the server wrote them', () => {
  it('shows net profit and ending cash as written, and has a check for every dollar on the row', async () => {
    await renderList();

    const figureOf = (label: string) => [...resultCell().querySelectorAll('div')].find((item) => item.querySelector('dt')?.textContent === label);
    const amounts = reads(screen.getByRole('button', { name: 'Open run 30' }).closest('tr')).match(/-?\$[\d,]+(\.\d+)?/g);

    expect.soft(reads(figureOf('Net profit') ?? null), 'net profit').toBe(`Net profit ${SHOWN}`);
    expect.soft(reads(figureOf('Ending cash') ?? null), 'ending cash').toBe(`Ending cash ${SHOWN}`);
    expect.soft(amounts, 'has a check above for every dollar on the row: nothing else carries one').toEqual([SHOWN, SHOWN]);
  });
});
