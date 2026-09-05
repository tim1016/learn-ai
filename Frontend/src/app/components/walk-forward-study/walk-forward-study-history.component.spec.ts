import { fireEvent, render, screen, waitFor } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { WalkForwardStudyHistoryComponent } from './walk-forward-study-history.component';
import { WalkForwardStudyService } from './walk-forward-study.service';
import type { WalkForwardStudySummary } from './walk-forward-study.types';

function row(overrides: Partial<WalkForwardStudySummary> = {}): WalkForwardStudySummary {
  return {
    id: 'study-1',
    strategy_key: 'sma_crossover',
    symbol: 'SPY',
    status: 'completed',
    job_id: 'job-1',
    created_at_ms: 1704171600000,
    finished_at_ms: 1704172600000,
    window_start_ms: 1735707600000,
    window_end_ms: 1751342400000,
    training_months: 12,
    test_months: 3,
    measure: 'sharpe_ratio',
    min_trades: 5,
    fold_count: 4,
    completed_folds: 4,
    failed_folds: 0,
    expected_backtests: 32,
    completed_backtests: 32,
    verdict: { label: 'still worked', reason: 'median fold retention 0.812 is at least 0.5', successful_folds: 4, defined_folds: 4, study_retention: 0.812, median_test_sharpe: 1.1, oos_trade_count: 40, based_on: 'based on 4 of 4 folds' },
    winner_changes: 1,
    incomplete: false,
    uncommitted_changes: false,
    failure_reason: null,
    ...overrides,
  };
}

describe('WalkForwardStudyHistoryComponent', () => {
  it('renders each study with its folds, status and verdict, and opens one', async () => {
    const list = vi.fn(async () => [row()]);
    const view = await render(WalkForwardStudyHistoryComponent, {
      inputs: { strategies: [{ name: 'sma_crossover', display_name: 'SMA Crossover', sweep_eligibility: { eligible: true, reason_codes: [], offending_parameters: [] } } as never] },
      providers: [{ provide: WalkForwardStudyService, useValue: { list, delete: vi.fn() } }],
    });
    const opened = vi.fn();
    view.fixture.componentInstance.opened.subscribe(opened);

    await waitFor(() => expect(screen.getByRole('cell', { name: 'SMA Crossover' })).not.toBeNull());
    expect(screen.getByText('4 / 4')).not.toBeNull();
    expect(screen.getByText('still worked')).not.toBeNull();
    expect(screen.getByText(/based on 4 of 4 folds/)).not.toBeNull();

    fireEvent.click(screen.getByRole('button', { name: /open study study-1/i }));
    expect(opened).toHaveBeenCalledWith('study-1');
  });

  it('filters through the service and deletes behind a confirmation', async () => {
    const list = vi.fn(async () => [row()]);
    const del = vi.fn(async () => undefined);
    await render(WalkForwardStudyHistoryComponent, { providers: [{ provide: WalkForwardStudyService, useValue: { list, delete: del } }] });
    await waitFor(() => expect(list).toHaveBeenCalled());

    fireEvent.change(screen.getByRole('combobox', { name: /filter by status/i }), { target: { value: 'failed' } });
    await waitFor(() => expect(list).toHaveBeenLastCalledWith({ status: 'failed' }));

    fireEvent.click(screen.getByRole('button', { name: /delete study study-1/i }));
    fireEvent.click(screen.getByRole('button', { name: /confirm delete/i }));
    await waitFor(() => expect(del).toHaveBeenCalledWith('study-1'));
    expect(screen.queryByRole('button', { name: /open study study-1/i })).toBeNull();
  });
});
