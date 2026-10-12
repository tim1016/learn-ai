import { screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { RunRefusedError } from '../options-backtest.service';
import type { RunDetail, RunListItem } from '../options-backtest.types';
import { tradesPage } from '../run-detail/testing/detail-fixtures';
import { fakeRunDetail } from '../run-detail/testing/fake-run-detail';
import { lettingGo } from '../testing/page-actions';
import { renderRunPanel } from '../testing/render-run';
import { fakeService } from '../testing/render-page';
import { runDetail, runItem } from '../testing/run-fixtures';

/** The closer look at a run (its trades, its export, one day) belongs to a complete run, and to no other. */

async function panelFor(item: RunListItem, detail: RunDetail) {
  const closer = fakeRunDetail(vi, { trades: vi.fn(async () => tradesPage()) });
  const service = fakeService(vi, { run: vi.fn(async () => detail) });
  await renderRunPanel(vi, item, { service, detail: closer });
  await lettingGo();
  return closer;
}

describe('the closer look at a run, only for a complete one', () => {
  it('shows the trades, the export and the day picker for a complete run, and reads its trades', async () => {
    const closer = await panelFor(runItem({ status: 'complete' }), runDetail());

    expect(await screen.findByRole('region', { name: 'Trades' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Export trades (CSV)' })).toBeTruthy();
    expect(screen.getByRole('region', { name: 'One day' })).toBeTruthy();
    expect(closer.trades).toHaveBeenCalledTimes(1);
  });

  it.each([
    ['running', runItem({ status: 'running' }), runDetail({ status: 'running', figures: null, headline: null })],
    ['failed', runItem({ status: 'failed', failure: { code: 'X', reason: 'No data.', items: [] } }), runDetail({ status: 'failed', figures: null, headline: null, failure: { code: 'X', reason: 'No data.', items: [] } })],
    ['cancelled', runItem({ status: 'cancelled' }), runDetail({ status: 'cancelled', figures: null, headline: null })],
    ['interrupted', runItem({ status: 'interrupted' }), runDetail({ status: 'interrupted', figures: null, headline: null })],
  ] as const)('shows none of it for a %s run, and reads no trade', async (_status, item, detail) => {
    const closer = await panelFor(item, detail);

    expect(screen.queryByRole('region', { name: 'Trades' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Export trades (CSV)' })).toBeNull();
    expect(screen.queryByRole('region', { name: 'One day' })).toBeNull();
    expect(closer.trades).not.toHaveBeenCalled();
  });

  it('shows none of it while the run is still being read', async () => {
    const closer = fakeRunDetail(vi, { trades: vi.fn(async () => tradesPage()) });
    const service = fakeService(vi, {
      run: vi.fn(async (id: number) => {
        throw new RunRefusedError('OPTIONS_BACKTEST_RUN_NOT_FOUND', `No run ${id}.`, 404);
      }),
    });
    await renderRunPanel(vi, runItem({ status: 'complete' }), { service, detail: closer });
    await lettingGo();

    expect(screen.queryByRole('region', { name: 'Trades' })).toBeNull();
    expect(closer.trades).not.toHaveBeenCalled();
  });
});
