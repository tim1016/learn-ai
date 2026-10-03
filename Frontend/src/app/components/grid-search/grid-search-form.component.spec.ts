import { fireEvent, render, screen, waitFor } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type { DateBounds } from '../../shared/date-picker';
import { sessionOpenMs } from '../../shared/date-picker/testing/fake-trading-calendar';
import { etIsoDate } from '../../shared/date/et-midnight';
import { fakePickerWorld } from '../../shared/symbol-picker/testing/fake-picker-world';
import { HeldGapsService } from '../../shared/ticker-catalog';
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
    providers: [...fakePickerWorld().providers, { provide: GridSearchService, useValue: { preflight: vi.fn(async (_spec: GridSearchSpecRequest) => PLAN), launch: vi.fn(async (_spec: GridSearchSpecRequest) => 'job-1'), ...service } }],
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

  it('refuses a malformed value list client-side: names the entry, disables launch, never preflights it', async () => {
    // #1940: ".3..4" was dropped silently and the shrunken grid was preflighted and launchable.
    const preflight = vi.fn(async (_spec: GridSearchSpecRequest) => PLAN);
    const view = await renderForm([strategy()], { preflight });

    fireEvent.click(screen.getByRole('checkbox', { name: /short window/i }));
    await view.fixture.whenStable();
    const calls = preflight.mock.calls.length;

    fireEvent.input(screen.getByLabelText(/values \(comma-separated\)/i), { target: { value: '5,.3..4,15' } });
    await view.fixture.whenStable();

    await waitFor(() => expect(screen.getAllByRole('alert').map((el) => el.textContent).join(' ')).toContain('".3..4" is not a number.'));
    await waitFor(() => expect(screen.getAllByRole('alert').map((el) => el.textContent).join(' ')).toMatch(/fix the values for/i));
    expect((screen.getByRole('button', { name: /launch search/i }) as HTMLButtonElement).disabled).toBe(true);
    expect(preflight.mock.calls.length).toBe(calls);
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
    // The window opens on the last 24 whole months the lake holds for SPY (held 2024-01-02 to 2026-09-18).
    expect([etIsoDate(spec?.start_ms ?? 0), etIsoDate((spec?.end_ms ?? 0) - 1)]).toEqual(['2024-09-19', '2026-09-18']);
  });

  it("reads the gaps in the symbol's held span and counts them under the window (#2858)", async () => {
    const gapsFor = vi.fn((_mode: string, symbol: string, bounds: DateBounds) =>
      symbol === 'SPY' && bounds.max !== null ? { unheldMs: new Set(['2025-03-03', '2025-03-04'].map(sessionOpenMs)), problem: null } : null,
    );
    await render(GridSearchFormComponent, {
      inputs: { strategies: [strategy()], preflightDebounceMs: 0 },
      providers: [...fakePickerWorld().providers, { provide: HeldGapsService, useValue: { gapsFor } }, { provide: GridSearchService, useValue: { preflight: vi.fn(async () => PLAN), launch: vi.fn() } }],
    });

    await waitFor(() => expect(screen.getByText(/sessions, 2 not held/)).not.toBeNull());
    expect(gapsFor).toHaveBeenCalledWith('polygon_split_adjusted', 'SPY', expect.objectContaining({ min: '2024-01-02', max: '2026-09-18' }), 'minute');
  });

  it('opens on as many whole months as a short history holds, and does not reopen a window the operator emptied (#2858)', async () => {
    const preflight = vi.fn(async (_spec: GridSearchSpecRequest) => PLAN);
    // Held for a little over three months: 24 whole months do not fit, three do.
    const world = fakePickerWorld([{ symbol: 'SPY', name: 'SPDR S&P 500', firstHeld: '2026-06-10', lastHeld: '2026-09-18' }]);
    const view = await render(GridSearchFormComponent, {
      inputs: { strategies: [strategy()], preflightDebounceMs: 0 },
      providers: [...world.providers, { provide: GridSearchService, useValue: { preflight, launch: vi.fn() } }],
    });
    await waitFor(() => expect(preflight).toHaveBeenCalled());
    const spec = preflight.mock.lastCall?.[0];
    expect([etIsoDate(spec?.start_ms ?? 0), etIsoDate((spec?.end_ms ?? 0) - 1)]).toEqual(['2026-06-19', '2026-09-18']);

    const start = screen.getByRole<HTMLInputElement>('textbox', { name: 'Start date' });
    const end = screen.getByRole<HTMLInputElement>('textbox', { name: 'End date' });
    fireEvent.change(start, { target: { value: '' } });
    fireEvent.change(end, { target: { value: '' } });
    await view.fixture.whenStable();

    expect([start.value, end.value]).toEqual(['', '']);
  });

  it('holds the launch while a date field holds no usable date, and says to pick the dates (#2858)', async () => {
    const preflight = vi.fn(async (_spec: GridSearchSpecRequest) => PLAN);
    await renderForm([strategy()], { preflight });
    await waitFor(() => expect(preflight).toHaveBeenCalled());
    const asked = preflight.mock.calls.length;

    fireEvent.change(screen.getByRole('textbox', { name: 'Start date' }), { target: { value: '2024-09' } });

    await waitFor(() => expect(screen.getByText('Pick the dates to search over.')).not.toBeNull());
    expect((screen.getByRole('button', { name: /launch search/i }) as HTMLButtonElement).disabled).toBe(true);
    expect(preflight.mock.calls.length).toBe(asked);
  });

  it('launches a decision-minute-open search that charges no fee unless asked (#2599, #2601)', async () => {
    const launch = vi.fn(async (_spec: GridSearchSpecRequest) => 'job-7');
    await renderForm([strategy()], { launch });

    expect(screen.getByText('Fees not charged')).toBeTruthy();
    expect((screen.getByLabelText('Fill mode') as HTMLSelectElement).value).toBe('decision_minute_open');
    await waitFor(() => expect((screen.getByRole('button', { name: /launch search/i }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole('button', { name: /launch search/i }));
    await waitFor(() => expect(launch).toHaveBeenCalled());

    const spec = launch.mock.lastCall?.[0];
    expect(spec?.fill_mode).toBe('decision_minute_open');
    expect(spec?.commission_per_order).toBe(0);
  });

  it('launches under another fill mode when the researcher picks one', async () => {
    const launch = vi.fn(async (_spec: GridSearchSpecRequest) => 'job-8');
    await renderForm([strategy()], { launch });

    fireEvent.change(screen.getByLabelText('Fill mode'), { target: { value: 'signal_bar_close' } });
    await waitFor(() => expect((screen.getByRole('button', { name: /launch search/i }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole('button', { name: /launch search/i }));
    await waitFor(() => expect(launch).toHaveBeenCalled());

    expect(launch.mock.lastCall?.[0].fill_mode).toBe('signal_bar_close');
  });
});
