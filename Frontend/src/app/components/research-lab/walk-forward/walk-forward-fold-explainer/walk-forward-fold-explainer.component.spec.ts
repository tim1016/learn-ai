import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it } from 'vitest';

import type { FoldResult } from '../../../../services/walk-forward.types';
import type { RunMetrics } from '../../../../services/strategy-runs.types';
import { WalkForwardFoldExplainerComponent } from './walk-forward-fold-explainer.component';

afterEach(() => {
  document.body.querySelectorAll('.p-dialog-mask').forEach((element) => element.remove());
});

const metrics: RunMetrics = {
  total_trades: 3,
  winning_trades: 2,
  losing_trades: 1,
  win_rate: 66.67,
  total_return_pct: 12.34,
  max_drawdown_pct: -4.56,
  sharpe_ratio: 1.5,
  sortino_ratio: null,
  profit_factor: null,
  expectancy_pct: null,
  payoff_ratio: null,
  exposure_pct: null,
  avg_trade_bars: null,
};

function fold(foldIndex: number): FoldResult {
  const start = Date.UTC(2026, 0, 1) + foldIndex * 30 * 86_400_000;
  return {
    fold_index: foldIndex,
    train_start_ms: start,
    train_end_ms: start + 20 * 86_400_000,
    test_start_ms: start + 20 * 86_400_000,
    test_end_ms: start + 30 * 86_400_000,
    test_run_id: null,
    test_metrics: metrics,
    test_trade_count: 3,
    status: 'completed',
    failure_reason: null,
    selected_parameters: { period: 10 },
    training_candidates: [],
    selected_train_sharpe: 1.2,
    oos_retention: null,
  };
}

async function renderExplainer() {
  await TestBed.configureTestingModule({
    imports: [WalkForwardFoldExplainerComponent],
    providers: [provideZonelessChangeDetection()],
  }).compileComponents();
  const fixture = TestBed.createComponent(WalkForwardFoldExplainerComponent);
  fixture.componentRef.setInput('folds', [fold(1), fold(2)]);
  fixture.componentRef.setInput('parameterLabel', 'period = 10');
  fixture.componentRef.setInput('fold', fold(1));
  fixture.detectChanges();
  return fixture;
}

describe('WalkForwardFoldExplainerComponent', () => {
  it('renders the fold header and chronology when a fold is open', async () => {
    const fixture = await renderExplainer();

    expect(document.body.textContent).toContain('Fold 1 of 2');
    expect(document.body.textContent).toContain('1 newer fold follows');
    expect(document.body.textContent).toContain('Folds are ordered by their out-of-sample test window.');
    expect(document.body.textContent).toContain('period = 10');

    fixture.componentRef.setInput('fold', null);
    fixture.detectChanges();
    expect(document.body.textContent).not.toContain('Fold 1 of 2');
  });

  it('labels the dialog for screen readers through the header template\'s aria id', async () => {
    await renderExplainer();

    // PrimeNG points the dialog container's aria-labelledby at a generated
    // id; with a custom header template the built-in title span is not
    // rendered, so the template itself must carry that id or the dialog is
    // unnamed.
    const dialog = document.body.querySelector<HTMLElement>('.fold-explainer-dialog');
    const labelledBy = dialog?.getAttribute('aria-labelledby');
    expect(labelledBy).toBeTruthy();
    expect(document.getElementById(labelledBy as string)?.textContent).toContain('Fold 1 of 2');
  });

  it('emits closed when the dialog\'s own close control is used', async () => {
    const fixture = await renderExplainer();
    let closed = false;
    fixture.componentInstance.closed.subscribe(() => (closed = true));

    document.body
      .querySelector<HTMLButtonElement>('.fold-explainer-dialog .p-dialog-close-button')
      ?.click();
    fixture.detectChanges();

    expect(closed).toBe(true);
  });
});
