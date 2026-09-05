import { ActivatedRoute, provideRouter } from '@angular/router';
import { render, screen, waitFor } from '@testing-library/angular';
import { of } from 'rxjs';
import { describe, expect, it, vi } from 'vitest';

import { GridSearchService } from '../grid-search/grid-search.service';
import { WalkForwardStudyPageComponent } from './walk-forward-study-page.component';
import { WalkForwardStudyService } from './walk-forward-study.service';

describe('WalkForwardStudyPageComponent', () => {
  it('seeds the form from ?search=, then opens a launched study by its job id', async () => {
    const list = vi.fn(async () => [{ id: 'study-9', status: 'queued' }]);
    const gridGet = vi.fn(async () => ({ id: 'grid-1', request: { strategy_key: 'sma_crossover', symbol: 'SPY', param_ranges: {}, start_ms: 0, end_ms: 1, resolution: 'minute', fill_mode: 'signal_bar_close', commission_per_order: 1, slippage_per_share: 0, initial_cash: 100000, measure: 'sharpe_ratio', min_trades: 5 } }));
    const view = await render(WalkForwardStudyPageComponent, {
      providers: [
        provideRouter([]),
        { provide: ActivatedRoute, useValue: { queryParamMap: of(new Map([['search', 'grid-1']])) } },
        { provide: GridSearchService, useValue: { loadStrategies: vi.fn(async () => []), get: gridGet, preflight: vi.fn() } },
        { provide: WalkForwardStudyService, useValue: { list, get: vi.fn(async () => ({ id: 'study-9', status: 'queued', folds: [], verdict: null, fold_count: 0, completed_folds: 0, failed_folds: 0, strategy_key: 'sma_crossover', symbol: 'SPY', window_start_ms: 0, window_end_ms: 1, training_months: 1, test_months: 1, measure: 'sharpe_ratio', min_trades: 5, created_at_ms: 0, expected_backtests: 0, completed_backtests: 0, winner_changes: 0, incomplete: false, uncommitted_changes: false, failure_reason: null, resumable: false, resume_refusal: null })), preflight: vi.fn() } },
      ],
    });

    expect(screen.getByRole('tab', { name: /new study/i })).not.toBeNull();
    expect(screen.getByRole('tab', { name: /history/i })).not.toBeNull();
    await waitFor(() => expect(gridGet).toHaveBeenCalledWith('grid-1'));
    await waitFor(() => expect(screen.getByRole('status').textContent).toContain('copied from grid search grid-1'));
    expect(view.fixture.componentInstance.prefill()?.symbol).toBe('SPY');

    await view.fixture.componentInstance.onLaunched({ jobId: 'job-7' });
    await view.fixture.whenStable();

    expect(list).toHaveBeenCalledWith({ job_id: 'job-7' });
    await waitFor(() => expect(view.fixture.componentInstance.openStudyId()).toBe('study-9'));
    expect(view.fixture.componentInstance.activeTab()).toBe('history');
  });
});
