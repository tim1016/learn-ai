import { fireEvent, render, screen, waitFor } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type { StrategyInfo } from '../strategy-lab/strategy-lab.models';
import { sweepableStrategy as strategy } from './testing/fixtures';
import { GridSearchFormComponent } from './grid-search-form.component';
import { GridSearchRefusedError, GridSearchService } from './grid-search.service';
import type { GridSearchPreflight, GridSearchSpecRequest } from './grid-search.types';

const PLAN: GridSearchPreflight = {
  strategy_key: 'sma_crossover',
  symbol: 'SPY',
  combinations: 3,
  total_backtests: 3,
  backtest_limit: 5000,
  estimated_seconds: 12.4,
  expected_sessions: 500,
  run_up: { data_start_ms: 1704171600000, evaluation_start_ms: 1705035600000, evaluation_end_ms: 1735621200000, required_samples: 31, bar_span_ms: 900000, run_up_sessions: 2, carved_from_range: true },
};

async function renderForm(strategies: StrategyInfo[], service: Partial<GridSearchService>) {
  const view = await render(GridSearchFormComponent, {
    inputs: { strategies, preflightDebounceMs: 0 },
    providers: [{ provide: GridSearchService, useValue: { preflight: vi.fn(async (_spec: GridSearchSpecRequest) => PLAN), launch: vi.fn(async (_spec: GridSearchSpecRequest) => 'job-1'), ...service } }],
  });
  return view;
}

describe('GridSearchFormComponent', () => {
  it('renders the selected strategy public parameters as fixed values with vary toggles', async () => {
    await renderForm([strategy()], {});

    expect(screen.getByText('Short window')).not.toBeNull();
    expect(screen.getByText('Long window')).not.toBeNull();
    expect(screen.queryByLabelText(/fixed value for symbol/i)).toBeNull();
    expect((screen.getByLabelText('Fixed value for Short window') as HTMLInputElement).value).toBe('10');
  });

  it('lists an ineligible strategy with its reason and offending parameters instead of offering it', async () => {
    await renderForm(
      [
        strategy(),
        strategy({ name: 'options_spread', display_name: 'Options Spread', sweep_eligibility: { eligible: false, reason_codes: ['NON_NUMERIC_PUBLIC_PARAMETER'], offending_parameters: ['spread_type'] } }),
      ],
      {},
    );

    const picker = screen.getByRole('combobox', { name: 'Strategy' }) as HTMLSelectElement;
    expect(Array.from(picker.options).map((o) => o.value)).toEqual(['sma_crossover']);
    const list = screen.getByRole('list', { name: /cannot be swept/i });
    expect(list.textContent).toContain('Options Spread');
    expect(list.textContent).toContain('spread_type');
  });

  it('reveals the range editor when a parameter is ticked to vary and preflights the count', async () => {
    const preflight = vi.fn(async (_spec: GridSearchSpecRequest) => ({ ...PLAN, total_backtests: 3 }));
    const view = await renderForm([strategy()], { preflight });

    fireEvent.click(screen.getByRole('checkbox', { name: /short window/i }));
    await view.fixture.whenStable();

    fireEvent.click(screen.getByRole('button', { name: /^range$/i }));
    fireEvent.input(screen.getByLabelText(/^low$/i), { target: { value: '5' } });
    fireEvent.input(screen.getByLabelText(/^high$/i), { target: { value: '15' } });
    fireEvent.input(screen.getByLabelText(/^step$/i), { target: { value: '5' } });

    await waitFor(() =>
      expect(preflight.mock.lastCall?.[0].param_ranges['short_window']).toEqual({ type: 'low_high_step', low: 5, high: 15, step: 5 }),
    );
    expect(preflight.mock.lastCall?.[0].param_ranges['long_window']).toEqual({ type: 'value_list', values: [30] });
    await waitFor(() => expect(screen.getByText(/backtests \(limit/).textContent).toContain('3'));
    expect(screen.getByText(/run-up uses the first 2 trading days/)).not.toBeNull();
  });

  it('surfaces a refusal and disables launch', async () => {
    const preflight = vi.fn(async (_spec: GridSearchSpecRequest) => {
      throw new GridSearchRefusedError({ code: 'WORKLOAD_LIMIT', message: '6000 backtests exceed the limit of 5000; narrow the grid' });
    });
    const launch = vi.fn(async (_spec: GridSearchSpecRequest) => 'job-1');
    await renderForm([strategy()], { preflight, launch });

    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('exceed the limit'));
    expect((screen.getByRole('button', { name: /launch search/i }) as HTMLButtonElement).disabled).toBe(true);
    expect(launch).not.toHaveBeenCalled();
  });

  it('launches the exact preflighted spec and emits the job id', async () => {
    const launch = vi.fn(async (_spec: GridSearchSpecRequest) => 'job-42');
    const view = await renderForm([strategy()], { launch });
    const launched = vi.fn();
    view.fixture.componentInstance.launched.subscribe(launched);

    await waitFor(() => expect((screen.getByRole('button', { name: /launch search/i }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole('button', { name: /launch search/i }));
    await waitFor(() => expect(launched).toHaveBeenCalledWith({ jobId: 'job-42' }));

    const spec = launch.mock.lastCall?.[0];
    expect(spec?.symbol).toBe('SPY');
    expect(spec?.measure).toBe('sharpe_ratio');
    expect(spec?.min_trades).toBe(5);
    expect(spec?.end_ms ?? 0).toBeGreaterThan(spec?.start_ms ?? 0);
  });
});
