import { fireEvent, render, screen, within } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import type {
  FoldResult,
  ParameterSearchConfig,
  TrainingCandidateResult,
} from '../../../../services/walk-forward.types';
import { WalkForwardParameterComparisonComponent } from './walk-forward-parameter-comparison.component';

const METRICS = {
  total_trades: 4,
  winning_trades: 3,
  losing_trades: 1,
  win_rate: 0.75,
  total_return_pct: 0.02,
  max_drawdown_pct: 0.01,
  sharpe_ratio: 1.5,
  sortino_ratio: 2,
  profit_factor: 2.5,
  expectancy_pct: 0.005,
  payoff_ratio: 2,
  exposure_pct: 0.1,
  avg_trade_bars: 5,
};

function trainingCandidate(gapBps: number, sharpe: number): TrainingCandidateResult {
  return {
    parameters: { gap_bps: gapBps },
    train_run_id: `train-${gapBps}`,
    train_metrics: {
      ...METRICS,
      total_trades: gapBps === 2 ? 6 : 3,
      total_return_pct: gapBps === 2 ? 0.0325 : -0.014,
      max_drawdown_pct: gapBps === 2 ? 0.0125 : 0.028,
      sharpe_ratio: sharpe,
    },
    receipt_persisted: true,
    eligible: true,
    ineligibility_reason: null,
  };
}

function fold(index: number, selectedGap: number, oosReturn: number): FoldResult {
  return {
    fold_index: index,
    train_start_ms: Date.UTC(2024, index, 2, 20),
    train_end_ms: Date.UTC(2024, index, 28, 20),
    test_start_ms: Date.UTC(2024, index + 1, 1, 20),
    test_end_ms: Date.UTC(2024, index + 1, 28, 20),
    test_run_id: `test-${index}`,
    test_metrics: { ...METRICS, total_return_pct: oosReturn },
    test_trade_count: 4,
    status: 'completed',
    failure_reason: null,
    selected_parameters: { gap_bps: selectedGap },
    training_candidates: [
      trainingCandidate(2, index === 0 ? 1.1 : 0.8),
      trainingCandidate(3, index === 0 ? 0.7 : -0.4),
    ],
    selected_train_sharpe: selectedGap === 2 ? 1.1 : 0.7,
    oos_retention: 0.5,
  };
}

const PARAMETER_SEARCH: ParameterSearchConfig = {
  objective: 'sharpe_ratio',
  min_trades: 1,
  candidates: [
    { parameters: { gap_bps: 2 }, strategy_spec_hash: 'gap-2', strategy_spec_json: {} },
    { parameters: { gap_bps: 3 }, strategy_spec_hash: 'gap-3', strategy_spec_json: {} },
  ],
};

describe('WalkForwardParameterComparisonComponent', () => {
  it('separates training candidate evidence from selected OOS outcomes', async () => {
    await render(WalkForwardParameterComparisonComponent, {
      inputs: {
        folds: [fold(0, 2, 0.012), fold(1, 3, -0.008)],
        parameterSearch: PARAMETER_SEARCH,
      },
    });

    const matrix = screen.getByRole('table', {
      name: /training sharpe for every parameter candidate/i,
    });
    expect(within(matrix).getByRole('rowheader', { name: '2 bps' })).not.toBeNull();
    expect(within(matrix).getByLabelText(/fold 1, 2 bps: training sharpe 1.10.*selected/i).textContent)
      .toMatch(/1.10.*chosen/i);
    expect(within(matrix).getByLabelText(/fold 2, 3 bps: training sharpe -0.40.*selected/i).textContent)
      .toMatch(/-0.40.*chosen/i);

    const outcomes = screen.getByRole('region', {
      name: /selected parameter and out-of-sample return by fold/i,
    });
    expect(within(outcomes).getByRole('link', { name: /fold 1, selected 2 bps, oos return 1.20%/i }))
      .not.toBeNull();
    expect(within(outcomes).getByRole('link', { name: /fold 2, selected 3 bps, oos return -0.80%/i }))
      .not.toBeNull();
    expect(screen.getByText(/unselected candidates have no oos result/i)).not.toBeNull();
    expect(screen.getByText(/risk-adjusted consistency, not portfolio growth/i)).not.toBeNull();
  });

  it('switches the same persisted matrix between return, drawdown, and trades', async () => {
    await render(WalkForwardParameterComparisonComponent, {
      inputs: {
        folds: [fold(0, 2, 0.012), fold(1, 3, -0.008)],
        parameterSearch: PARAMETER_SEARCH,
      },
    });

    fireEvent.click(screen.getByRole('button', { name: 'Net return' }));
    const returnMatrix = screen.getByRole('table', {
      name: /training net return for every parameter candidate/i,
    });
    expect(within(returnMatrix).getByLabelText(/fold 1, 2 bps: training net return 3.25%/i).textContent)
      .toContain('3.25%');
    expect(within(returnMatrix).getByLabelText(/fold 1, 3 bps: training net return -1.40%/i).textContent)
      .toContain('-1.40%');

    fireEvent.click(screen.getByRole('button', { name: 'Max drawdown' }));
    const drawdownMatrix = screen.getByRole('table', {
      name: /training max drawdown for every parameter candidate/i,
    });
    expect(within(drawdownMatrix).getByLabelText(/fold 1, 2 bps: training max drawdown 1.25%/i).textContent)
      .toContain('1.25%');

    fireEvent.click(screen.getByRole('button', { name: 'Trades' }));
    const tradeMatrix = screen.getByRole('table', {
      name: /training trades for every parameter candidate/i,
    });
    expect(within(tradeMatrix).getByLabelText(/fold 1, 2 bps: training trades 6/i).textContent)
      .toContain('6');
    expect(screen.getByRole('button', { name: 'Trades' }).getAttribute('aria-pressed'))
      .toBe('true');
  });

  it('explains a fold date boundary and its recency inside the receipt', async () => {
    await render(WalkForwardParameterComparisonComponent, {
      inputs: {
        folds: [fold(0, 2, 0.012), fold(1, 3, -0.008)],
        parameterSearch: PARAMETER_SEARCH,
      },
    });

    expect(screen.getByText(/f1 is oldest and the highest-numbered fold is newest/i))
      .not.toBeNull();
    expect(screen.getByRole('button', { name: /explain fold 2, most recent fold/i }))
      .not.toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Explain fold 1' }));

    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getByText('Fold 1 of 2')).not.toBeNull();
    expect(within(dialog).getByText('Oldest fold')).not.toBeNull();
    expect(within(dialog).getByText('2024-01-02 → 2024-01-28')).not.toBeNull();
    expect(within(dialog).getByText('2024-02-01 → 2024-02-28')).not.toBeNull();
    expect(within(dialog).getByText(/1 newer fold follows this one in the receipt/i)).not.toBeNull();
    expect(within(dialog).getByText(/latest inside this frozen analysis—not live market data/i))
      .not.toBeNull();
    expect(within(dialog).getByText('2 bps')).not.toBeNull();
    expect(within(dialog).getByRole('button', { name: 'Close fold explanation' }))
      .not.toBeNull();
  });

  it('renders missing candidate receipts without inventing a score', async () => {
    const missing = fold(0, 2, 0.01);
    missing.training_candidates = [trainingCandidate(2, 1.1)];

    await render(WalkForwardParameterComparisonComponent, {
      inputs: { folds: [missing], parameterSearch: PARAMETER_SEARCH },
    });

    expect(screen.getByLabelText(/fold 1, 3 bps: no persisted training result/i).textContent)
      .toContain('—');
  });
});
