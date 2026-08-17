import { PercentPipe } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import type { FoldResult } from '../../../../services/walk-forward.types';

export interface SelectedFoldOutcome {
  fold: FoldResult;
  parameterLabel: string;
}

/** Visualizes only the frozen candidate that actually advanced to OOS. */
@Component({
  selector: 'app-walk-forward-selected-outcomes',
  imports: [PercentPipe, RouterLink],
  templateUrl: './walk-forward-selected-outcomes.component.html',
  styleUrl: './walk-forward-selected-outcomes.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class WalkForwardSelectedOutcomesComponent {
  readonly outcomes = input.required<readonly SelectedFoldOutcome[]>();

  readonly maxAbsoluteReturn = computed(() => Math.max(
    ...this.outcomes()
      .filter(({ fold }) => fold.status === 'completed')
      .map(({ fold }) => Math.abs(fold.test_metrics.total_return_pct)),
    0,
  ));

  outcomeAriaLabel(outcome: SelectedFoldOutcome): string {
    const { fold, parameterLabel } = outcome;
    const foldLabel = `Fold ${fold.fold_index + 1}`;
    if (fold.status !== 'completed') {
      return `${foldLabel}, selected ${parameterLabel}, OOS test failed`;
    }
    return `${foldLabel}, selected ${parameterLabel}, OOS return ${formatPercent(fold.test_metrics.total_return_pct)}`;
  }

  returnBarHeight(fold: FoldResult): number {
    const maximum = this.maxAbsoluteReturn();
    if (maximum === 0 || fold.status !== 'completed') return 0;
    return Math.max(3, Math.abs(fold.test_metrics.total_return_pct) / maximum * 46);
  }
}

function formatPercent(value: number): string {
  return new Intl.NumberFormat('en-US', {
    style: 'percent',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value);
}
