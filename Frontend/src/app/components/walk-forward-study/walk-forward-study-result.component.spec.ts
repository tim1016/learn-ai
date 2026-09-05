import { provideRouter } from '@angular/router';
import { fireEvent, render, screen, waitFor } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { JobsService } from '../../services/jobs.service';
import { GridSearchService } from '../grid-search/grid-search.service';
import { WalkForwardStudyResultComponent } from './walk-forward-study-result.component';
import { WalkForwardStudyService } from './walk-forward-study.service';
import type { Fold, WalkForwardStudyDetail } from './walk-forward-study.types';

function fold(overrides: Partial<Fold> = {}): Fold {
  return {
    fold_index: 0,
    train_start_ms: 1735707600000,
    train_end_ms: 1743480000000,
    test_start_ms: 1743480000000,
    test_end_ms: 1746072000000,
    status: 'completed',
    train_search_id: 'train-0',
    test_search_id: 'test-0',
    winner_params_hash: 'h1',
    winner_params: { short_window: 5, long_window: 30 },
    train_sharpe: 1.5,
    test_sharpe: 1.0,
    test_trades: 12,
    retention: 0.6667,
    failure_reason: null,
    ...overrides,
  };
}

function detail(overrides: Partial<WalkForwardStudyDetail> = {}): WalkForwardStudyDetail {
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
    training_months: 3,
    test_months: 1,
    measure: 'sharpe_ratio',
    min_trades: 5,
    fold_count: 2,
    completed_folds: 1,
    failed_folds: 1,
    expected_backtests: 8,
    completed_backtests: 8,
    verdict: { label: 'could not be judged', reason: '1 of 2 folds failed; the out-of-sample record has holes', successful_folds: 1, defined_folds: 1, study_retention: 0.6667, median_test_sharpe: 1.0, oos_trade_count: 12, based_on: 'based on 1 of 1 folds' },
    winner_changes: 0,
    incomplete: false,
    uncommitted_changes: false,
    failure_reason: null,
    request: {} as never,
    receipt: {},
    folds: [fold(), fold({ fold_index: 1, status: 'failed', test_search_id: null, winner_params: null, train_sharpe: null, test_sharpe: null, test_trades: 0, retention: null, failure_reason: 'NO_ELIGIBLE_CANDIDATE: no candidate was eligible to win the training window' })],
    resumable: false,
    resume_refusal: 'the study is complete',
    ...overrides,
  };
}

async function renderResult(row: WalkForwardStudyDetail, overrides: Partial<WalkForwardStudyService> = {}) {
  const gridGet = vi.fn(async (id: string) => ({ id, owner: { kind: 'walk_forward', owner_id: 'study-1', fold_index: 0, phase: 'test' }, strategy_key: 'sma_crossover', symbol: 'SPY', status: 'completed', job_id: null, created_at_ms: 0, finished_at_ms: 0, window_start_ms: 0, window_end_ms: 1, measure: 'sharpe_ratio', min_trades: 5, expected_cells: 1, completed_cells: 1, failed_cells: 0, leader_params_hash: 'h1', leader_params: { short_window: 5 }, incomplete: false, uncommitted_changes: false, failure_reason: null, request: {}, receipt: {}, resumable: false, resume_refusal: null }));
  const view = await render(WalkForwardStudyResultComponent, {
    inputs: { studyId: row.id, pollMs: 0 },
    providers: [
      provideRouter([]),
      { provide: WalkForwardStudyService, useValue: { get: vi.fn(async () => row), finish: vi.fn(), delete: vi.fn(async () => undefined), ...overrides } },
      { provide: GridSearchService, useValue: { get: gridGet, cells: vi.fn(async () => ({ total: 0, page: 1, page_size: 25, sort_by: 'sharpe_ratio', direction: 'desc', cells: [] })) } },
      { provide: JobsService, useValue: { cancelJob: vi.fn(async () => undefined) } },
    ],
  });
  return { view, gridGet };
}

describe('WalkForwardStudyResultComponent', () => {
  it('renders the verdict with its coverage and one row per fold with winner, Sharpes, retention and failure reason', async () => {
    await renderResult(detail());

    await waitFor(() => expect(screen.getByRole('heading', { name: 'could not be judged' })).not.toBeNull());
    expect(screen.getByText(/based on 1 of 1 folds/)).not.toBeNull();
    expect(screen.getByText('short_window=5, long_window=30')).not.toBeNull();
    expect(screen.getAllByText('67%').length).toBe(2); // the verdict's median and fold 1's own retention
    expect(screen.getByText(/no candidate was eligible/)).not.toBeNull();
    expect(screen.getByText(/out of sample/)).not.toBeNull();
  });

  it('opens a fold sweep in place through the Grid Search result view and returns to the study', async () => {
    const { gridGet } = await renderResult(detail());
    await waitFor(() => expect(screen.getByRole('button', { name: /open test sweep of fold 1/i })).not.toBeNull());

    fireEvent.click(screen.getByRole('button', { name: /open test sweep of fold 1/i }));

    await waitFor(() => expect(gridGet).toHaveBeenCalledWith('test-0'));
    expect(screen.getByText(/only the winner's row is evidence/)).not.toBeNull();
    fireEvent.click(screen.getByRole('button', { name: /back to the study/i }));
    await waitFor(() => expect(screen.getByRole('heading', { name: 'could not be judged' })).not.toBeNull());
  });

  it('offers Finish only when the study is resumable and deletes behind a confirmation', async () => {
    const del = vi.fn(async () => undefined);
    const { view } = await renderResult(detail({ status: 'interrupted', incomplete: true, verdict: null, resumable: true, resume_refusal: null }), { delete: del });
    const deleted = vi.fn();
    view.fixture.componentInstance.deleted.subscribe(deleted);

    await waitFor(() => expect(screen.getByRole('button', { name: /finish/i })).not.toBeNull());
    fireEvent.click(screen.getByRole('button', { name: /^delete$/i }));
    fireEvent.click(screen.getByRole('button', { name: /confirm delete/i }));
    await waitFor(() => expect(deleted).toHaveBeenCalledWith('study-1'));
    expect(del).toHaveBeenCalledWith('study-1');
  });
});
