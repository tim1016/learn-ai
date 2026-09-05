import { fireEvent, render, screen, waitFor } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { GridSearchRefusedError } from '../grid-search/grid-search.service';
import { sweepableStrategy as strategy } from '../grid-search/testing/fixtures';
import type { GridSearchSpecRequest } from '../grid-search/grid-search.types';
import { WalkForwardStudyFormComponent } from './walk-forward-study-form.component';
import { WalkForwardStudyService } from './walk-forward-study.service';
import type { WalkForwardStudyPreflight, WalkForwardStudySpecRequest } from './walk-forward-study.types';

const PLAN: WalkForwardStudyPreflight = {
  strategy_key: 'sma_crossover',
  symbol: 'SPY',
  combinations: 4,
  fold_count: 3,
  total_backtests: 24,
  backtest_limit: 5000,
  estimated_seconds: 40,
  required_samples: 31,
  run_up_sessions: 2,
  folds: [
    { fold_index: 0, train_start_ms: 1735707600000, train_end_ms: 1743480000000, test_start_ms: 1743480000000, test_end_ms: 1746072000000 },
    { fold_index: 1, train_start_ms: 1738386000000, train_end_ms: 1746072000000, test_start_ms: 1746072000000, test_end_ms: 1748750400000 },
    { fold_index: 2, train_start_ms: 1740805200000, train_end_ms: 1748750400000, test_start_ms: 1748750400000, test_end_ms: 1751342400000 },
  ],
};

const PREFILL: GridSearchSpecRequest = {
  strategy_key: 'sma_crossover',
  symbol: 'QQQ',
  param_ranges: { short_window: { type: 'low_high_step', low: 5, high: 15, step: 5 }, long_window: { type: 'value_list', values: [40] } },
  start_ms: 1735707600000,
  end_ms: 1751342400000,
  resolution: 'minute',
  fill_mode: 'signal_bar_close',
  commission_per_order: 0.5,
  slippage_per_share: 0,
  initial_cash: 50_000,
  measure: 'net_profit',
  min_trades: 3,
};

async function renderForm(service: Partial<WalkForwardStudyService>, inputs: Record<string, unknown> = {}) {
  return render(WalkForwardStudyFormComponent, {
    inputs: { strategies: [strategy()], preflightDebounceMs: 0, ...inputs },
    providers: [
      { provide: WalkForwardStudyService, useValue: { preflight: vi.fn(async (_spec: WalkForwardStudySpecRequest) => PLAN), launch: vi.fn(async (_spec: WalkForwardStudySpecRequest) => 'job-1'), ...service } },
    ],
  });
}

describe('WalkForwardStudyFormComponent', () => {
  it('preflights the embedded grid plus the two month lengths and shows the fold plan', async () => {
    const preflight = vi.fn(async (_spec: WalkForwardStudySpecRequest) => PLAN);
    await renderForm({ preflight });

    await waitFor(() => expect(preflight).toHaveBeenCalled());
    const spec = preflight.mock.lastCall?.[0];
    expect(spec?.training_months).toBe(12);
    expect(spec?.test_months).toBe(3);
    expect(spec?.strategy_key).toBe('sma_crossover');
    expect(spec?.param_ranges['short_window']).toEqual({ type: 'value_list', values: [10] });
    await waitFor(() => expect(screen.getByText(/4 combinations × 3 folds × 2 windows = 24 backtests/)).not.toBeNull());
    expect(screen.getByText('Fold calendar')).not.toBeNull();
  });

  it('re-preflights when a month length changes and refuses non-positive lengths client-side', async () => {
    const preflight = vi.fn(async (_spec: WalkForwardStudySpecRequest) => PLAN);
    await renderForm({ preflight });
    await waitFor(() => expect(preflight).toHaveBeenCalled());

    fireEvent.input(screen.getByLabelText(/test window \(months\)/i), { target: { value: '6' } });
    await waitFor(() => expect(preflight.mock.lastCall?.[0].test_months).toBe(6));

    const before = preflight.mock.calls.length;
    fireEvent.input(screen.getByLabelText(/training window \(months\)/i), { target: { value: '0' } });
    await new Promise((resolve) => setTimeout(resolve, 5));
    expect(preflight.mock.calls.length).toBe(before);
    expect(screen.getByRole('alert').textContent).toContain('whole months');
    expect((screen.getByRole('button', { name: /launch study/i }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('shows a fold refusal with the nearest valid ends and disables launch', async () => {
    const preflight = vi.fn(async (_spec: WalkForwardStudySpecRequest) => {
      throw new GridSearchRefusedError({ code: 'FOLDS_INVALID', message: '14 months do not divide into whole folds of 3 months after 12 months of training; nearest valid end dates: 2025-01-01, 2025-04-01' });
    });
    const launch = vi.fn(async (_spec: WalkForwardStudySpecRequest) => 'job-1');
    await renderForm({ preflight, launch });

    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('nearest valid end dates'));
    expect((screen.getByRole('button', { name: /launch study/i }) as HTMLButtonElement).disabled).toBe(true);
    expect(launch).not.toHaveBeenCalled();
  });

  it('seeds the grid from a prefilled search and launches the exact preflighted spec', async () => {
    const preflight = vi.fn(async (_spec: WalkForwardStudySpecRequest) => PLAN);
    const launch = vi.fn(async (_spec: WalkForwardStudySpecRequest) => 'job-42');
    const view = await renderForm({ preflight, launch }, { prefill: PREFILL });
    const launched = vi.fn();
    view.fixture.componentInstance.launched.subscribe(launched);

    await waitFor(() => expect(preflight.mock.lastCall?.[0].symbol).toBe('QQQ'));
    const spec = preflight.mock.lastCall?.[0];
    expect(spec?.param_ranges['short_window']).toEqual({ type: 'low_high_step', low: 5, high: 15, step: 5 });
    expect(spec?.param_ranges['long_window']).toEqual({ type: 'value_list', values: [40] });
    expect(spec?.measure).toBe('net_profit');
    expect(spec?.min_trades).toBe(3);
    expect(spec?.start_ms).toBe(PREFILL.start_ms);
    expect(spec?.end_ms).toBe(PREFILL.end_ms);

    await waitFor(() => expect((screen.getByRole('button', { name: /launch study/i }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole('button', { name: /launch study/i }));
    await waitFor(() => expect(launched).toHaveBeenCalledWith({ jobId: 'job-42' }));
    expect(launch.mock.lastCall?.[0]).toEqual(spec);
  });
});
