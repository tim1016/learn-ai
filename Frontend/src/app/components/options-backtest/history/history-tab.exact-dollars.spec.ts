import { render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { JobsService } from '../../../services/jobs.service';
import { fakePickerWorld } from '../../../shared/symbol-picker/testing/fake-picker-world';
import { OptionsBacktestService } from '../options-backtest.service';
import { RUN_POLL_MS } from '../run-poll';
import { fakeJobs } from '../testing/fake-jobs';
import { reads } from '../testing/page-actions';
import { fakeService } from '../testing/render-page';
import { runItem } from '../testing/run-fixtures';
import { HistoryTabComponent } from './history-tab.component';

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
  await render(HistoryTabComponent, {
    inputs: { active: true },
    providers: [...fakePickerWorld().providers, { provide: OptionsBacktestService, useValue: service }, { provide: JobsService, useValue: fakeJobs().service }, { provide: RUN_POLL_MS, useValue: 0 }],
  });
  await screen.findByRole('button', { name: 'Open run 30' });
}

const resultCell = () => (screen.getByRole('button', { name: 'Open run 30' }).closest('tr') as HTMLElement).querySelectorAll('td')[4] as HTMLElement;

describe('the dollars of a run in the list are shown exactly as the server wrote them', () => {
  it.each([
    ['net profit', 'Net profit'],
    ['ending cash', 'Ending cash'],
  ])('%s', async (_place, label) => {
    await renderList();

    const figure = [...resultCell().querySelectorAll('div')].find((item) => item.querySelector('dt')?.textContent === label);
    expect(reads(figure ?? null)).toBe(`${label} ${SHOWN}`);
  });

  it('has a row above for every dollar on the row: nothing else carries one', async () => {
    await renderList();

    const amounts = reads(screen.getByRole('button', { name: 'Open run 30' }).closest('tr')).match(/-?\$[\d,]+(\.\d+)?/g);
    expect(amounts).toEqual([SHOWN, SHOWN]);
  });
});
