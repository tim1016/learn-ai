import { fireEvent, screen, waitFor } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it, vi } from 'vitest';

import { etMidnightMs } from '../../shared/date/et-midnight';
import { sessionOpenMs } from '../../shared/date-picker/testing/fake-trading-calendar';
import { dataCheck, sessionCheck } from './testing/fixtures';
import { checkButton, openExample, pickDates, press } from './testing/page-actions';
import { fakeService, renderPage } from './testing/render-page';
import { runDetail, runItem } from './testing/run-fixtures';
import { scriptedRun } from './testing/run-script';

async function violations(): Promise<string[]> {
  const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });
  return results.violations.map((violation) => `${violation.id}: ${violation.nodes.map((node) => node.html).join(' | ')}`);
}

describe('the run part of the page for a screen reader', () => {
  it('has no accessibility violations with the dates picked and every part of a Check data answer showing', async () => {
    const answer = dataCheck(
      [
        sessionCheck('2026-09-14', { verdict: 'cannot_be_tested', problems: [{ code: 'older_than_the_data_plan', message: 'Older than the plan.', lake_reason: null }], notes: ['A note.'] }),
        sessionCheck('2026-09-15', { verdict: 'needs_download', missing: ['underlying_bars', 'contracts'], contracts: { state: 'known', needed: 9, missing: 4 } }),
        sessionCheck('2026-09-16', { skip: 'window_does_not_fit' }),
        sessionCheck('2026-09-17'),
      ],
      {
        longest_testable_stretch: {
          start_ms: etMidnightMs('2026-09-15'),
          end_ms: etMidnightMs('2026-09-18'),
          first_session_open_ms: sessionOpenMs('2026-09-15'),
          last_session_open_ms: sessionOpenMs('2026-09-17'),
          sessions: 3,
        },
      },
    );
    await renderPage(fakeService(vi, { checkData: vi.fn(async () => answer) }));
    await openExample();
    pickDates('2026-09-14', '2026-09-17');
    await waitFor(() => expect(checkButton().disabled).toBe(false));

    fireEvent.click(checkButton());
    await screen.findByRole('button', { name: 'Run available dates' });

    expect(await violations()).toEqual([]);
  });

  async function startedRun() {
    const setup = scriptedRun(vi);
    await renderPage(setup.service, undefined, { jobs: setup.jobs });
    await openExample();
    pickDates('2026-09-14', '2026-09-18');
    const start = screen.getByRole<HTMLButtonElement>('button', { name: 'Start backtest' });
    await waitFor(() => expect(start.disabled).toBe(false));
    fireEvent.click(start);
    await screen.findByRole('region', { name: 'Backtest run' });
    return setup;
  }

  it('has no accessibility violations while a backtest is going', async () => {
    const setup = await startedRun();
    setup.jobs.emit('job-1', { type: 'job.phase', phase: 'replaying', friendly: 'Replaying each session' });
    setup.jobs.emit('job-1', { type: 'job.progress', current: 2, total: 5, unit: 'sessions' });
    await screen.findByText('Sessions done: 2 of 5');

    expect(await violations()).toEqual([]);
  });

  it('has no accessibility violations with the result of a run showing', async () => {
    const setup = await startedRun();
    setup.service.run.mockResolvedValue(runDetail());

    setup.jobs.emit('job-1', { type: 'job.completed', result_url: '/x' });
    await screen.findByRole('region', { name: 'Headline figures' });

    expect(await violations()).toEqual([]);
  });

  it('has no accessibility violations with a failed run showing, its items grouped, one group open and one shut', async () => {
    const setup = await startedRun();
    const day = (offset: number) => sessionOpenMs('2026-09-14') + offset * 86_400_000;
    const failure = {
      code: 'DOWNLOAD_FAILED',
      reason: 'The market data for some sessions could not be fetched.',
      items: [
        { session_open_ms: day(0), code: 'listing_not_captured', message: 'No listing was stored.' },
        ...Array.from({ length: 8 }, (_, at) => ({ session_open_ms: day(at), code: 'took_too_long', message: 'It took longer than the run waits.' })),
        { session_open_ms: null, code: 'internal_error', message: 'The run stopped early.' },
      ],
    };
    setup.service.run.mockResolvedValue(runDetail({ status: 'failed', failure, figures: null, slippage: [], slippage_tolerance: null, headline: null }));

    setup.jobs.emit('job-1', { type: 'job.failed', code: 'RunFailedError', message: failure.reason });
    await screen.findByRole('list', { name: 'What failed' });

    expect(await violations()).toEqual([]);
    fireEvent.click(screen.getByRole('button', { name: /Took Too Long/ }));
    expect(await violations()).toEqual([]);
  });

  it('has no accessibility violations in History, with the list and with a run opened', async () => {
    const complete = runItem({ id: 30, job_id: 'job-30', status: 'complete', setup_name: 'Straddle', headline: { sessions_tested: 10, sessions_traded: 8, trades: 12, trades_won: 7, net_profit_usd: '1234.56', ending_cash_usd: '11234.56' } });
    await renderPage(fakeService(vi, { runs: vi.fn(async () => [complete]), run: vi.fn(async () => runDetail({ ...complete })) }));
    fireEvent.click(screen.getByRole('tab', { name: 'History' }));
    await screen.findByRole('button', { name: 'Open run 30' });
    expect(await violations()).toEqual([]);

    press('Open run 30');
    await screen.findByRole('region', { name: 'Headline figures' });
    await screen.findByText('Window for SPY.');

    expect(await violations()).toEqual([]);
  });
});
