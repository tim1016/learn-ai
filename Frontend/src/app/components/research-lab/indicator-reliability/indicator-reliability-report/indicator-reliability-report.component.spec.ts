import { render, screen, within } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import type {
  DecayCurvePoint,
  HorizonICResult,
  IndicatorReliabilityResponse,
} from '../indicator-reliability.models';
import { IndicatorReliabilityReportComponent } from './indicator-reliability-report.component';

// Canvas drawing is Chart.js's job and jsdom has no canvas: the report's own output is the DOM around the charts.
vi.mock('chart.js', () => ({
  Chart: Object.assign(
    vi.fn(function () {
      return { destroy: vi.fn() };
    }),
    { register: vi.fn() },
  ),
  registerables: [],
}));

function horizonRow(horizon: number, overrides: Partial<HorizonICResult> = {}): HorizonICResult {
  return {
    horizon,
    is_mean_ic: 0.02,
    is_t_stat: 1.1,
    is_p_value: 0.3,
    is_nw_t_stat: 1.0,
    is_nw_p_value: 0.32,
    is_effective_n: 300,
    oos_mean_ic: 0.015,
    oos_t_stat: 0.9,
    oos_p_value: 0.4,
    oos_effective_n: 120,
    oos_retention: 0.75,
    bonferroni_p: 0.9,
    fdr_p: 0.4,
    random_baseline_mean: 0,
    random_baseline_std: 0.01,
    ic_vs_random_zscore: 1.2,
    is_interpretation: '',
    oos_interpretation: null,
    is_hit_rate: 0.51,
    is_daily_ic_std: 0.05,
    strength_label: 'Weak',
    stability_label: 'Moderate',
    direction_label: 'Momentum',
    retention_delta_pct: -25,
    slope_adds_value: null,
    slope_recommended: null,
    annualized_ir: 0.4,
    sharpe_estimate: 0.3,
    breadth_per_year: 250,
    random_baseline_distribution: [],
    ...overrides,
  };
}

/** A successful run whose best horizon clears every test, so the verdict is "Ready to trade". */
function strongResult(): IndicatorReliabilityResponse {
  const decay: DecayCurvePoint[] = [1, 5].map((horizon) => ({
    horizon,
    ic: horizon === 5 ? 0.12 : 0.03,
    p_value: 0.01,
    ic_stderr: 0.01,
  }));
  return {
    success: true,
    ticker: 'SPY',
    indicator_name: 'rsi',
    indicator_params: { period: 14 },
    display_name: 'RSI(14)',
    category: 'momentum',
    start_date: '2024-01-02',
    end_date: '2024-06-28',
    bar_count: 12345,
    train_start: '2024-01-02',
    train_end: '2024-05-01',
    test_start: '2024-05-02',
    test_end: '2024-06-28',
    train_bars: 8000,
    test_bars: 4345,
    train_ratio: 0.7,
    results: [
      horizonRow(1),
      horizonRow(5, {
        is_mean_ic: 0.12,
        oos_mean_ic: 0.11,
        fdr_p: 0.001,
        ic_vs_random_zscore: 4.2,
        retention_delta_pct: -8,
        strength_label: 'Strong',
      }),
    ],
    slope_results: null,
    daily_ic_values: [],
    daily_ic_dates: [],
    best_horizon: 5,
    any_significant_after_bonferroni: true,
    any_significant_after_fdr: true,
    num_horizons_tested: 2,
    random_simulations: 100,
    verdict: null,
    decay_curve: decay,
    regime_results: null,
    next_steps: [],
    info_footnotes: [],
    warnings: [],
    error: null,
  };
}

const SERVER_VERDICT = {
  direction: 'Momentum',
  strength: 'Strong',
  stability: 'Moderate',
  tradeability: 'Marginal',
  horizon: 5,
  tradeability_caveat: 'Costs and turnover are not modelled.',
} as const;

const follows = (first: Element, second: Element) =>
  Boolean(first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING);

describe('IndicatorReliabilityReportComponent', () => {
  it('leads with the server’s own assessment and its tradeability caveat, ahead of the verdict this page computes', async () => {
    const { container } = await render(IndicatorReliabilityReportComponent, {
      inputs: { result: { ...strongResult(), verdict: { ...SERVER_VERDICT } } },
    });

    const assessment = container.querySelector('[data-testid="server-assessment"]');
    const note = container.querySelector('[data-testid="page-computed-note"]');
    if (assessment === null || note === null) throw new Error('assessment or note missing');
    expect.soft(assessment.textContent).toContain('Marginal');
    expect.soft(assessment.textContent).toContain('Costs and turnover are not modelled.');
    expect.soft(assessment.textContent).toContain('Momentum');
    expect.soft(assessment.textContent).toContain('5-bar');
    expect.soft(note.textContent).toContain('Computed in this page, not by the server.');
    expect.soft(follows(assessment, note), 'the note comes after the server’s assessment').toBe(true);
    // The page's own call and its Confidence gauge both sit below the note, not above the server.
    for (const computed of [screen.getByText('Ready to trade'), screen.getByText('Confidence')]) {
      expect.soft(follows(note, computed)).toBe(true);
    }
  });

  it('says “not recorded” when the result carries no server assessment, instead of showing none', async () => {
    const { container } = await render(IndicatorReliabilityReportComponent, { inputs: { result: strongResult() } });

    expect(container.querySelector('[data-testid="server-assessment"]')?.textContent).toContain(
      'Not recorded: this result carries no assessment from the server.',
    );
  });

  it('shows the verdict and marks the best-horizon row in the horizon table', async () => {
    await render(IndicatorReliabilityReportComponent, { inputs: { result: strongResult() } });

    expect(screen.getByText('Ready to trade')).toBeTruthy();
    const table = screen.getByRole('grid', { name: 'IC by forward horizon' });
    const best = within(table).getByText('BEST').closest('tr');
    if (best === null) throw new Error('no best-horizon row');
    expect(within(best).getByText('5-bar')).toBeTruthy();
    expect(within(best).getByText('0.1200')).toBeTruthy();
  });

  it('offers no re-run action by default, since a stored result has no study to run', async () => {
    await render(IndicatorReliabilityReportComponent, { inputs: { result: strongResult() } });

    expect(screen.queryByText('Run on another ticker')).toBeNull();
  });

  it('hands the re-run action to its host when the host can run the study', async () => {
    const runOnAnotherTicker = vi.fn();
    await render(IndicatorReliabilityReportComponent, {
      inputs: { result: strongResult(), canRerun: true },
      on: { runOnAnotherTicker },
    });

    await userEvent.setup().click(screen.getByText('Run on another ticker'));

    expect(runOnAnotherTicker).toHaveBeenCalledTimes(1);
  });
});
