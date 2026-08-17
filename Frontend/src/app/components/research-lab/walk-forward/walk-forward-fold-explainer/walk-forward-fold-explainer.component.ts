import { DecimalPipe, PercentPipe } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  output,
} from '@angular/core';
import { DialogModule } from 'primeng/dialog';

import type { FoldResult } from '../../../../services/walk-forward.types';
import { TimestampDisplayPipe } from '../../../../shared/timestamp';

interface FoldRecency {
  position: number;
  total: number;
  newerCount: number;
  label: string;
  newerFoldCopy: string;
}

/** Explains one fold's chronology and evidence boundary in plain language. */
@Component({
  selector: 'app-walk-forward-fold-explainer',
  imports: [DecimalPipe, DialogModule, PercentPipe, TimestampDisplayPipe],
  templateUrl: './walk-forward-fold-explainer.component.html',
  styleUrl: './walk-forward-fold-explainer.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class WalkForwardFoldExplainerComponent {
  readonly fold = input<FoldResult | null>(null);
  readonly folds = input.required<readonly FoldResult[]>();
  readonly parameterLabel = input.required<string>();
  readonly closed = output();

  readonly recency = computed<FoldRecency | null>(() => {
    const fold = this.fold();
    if (fold === null) return null;

    const folds = this.folds();
    const position = folds.findIndex((candidate) =>
      candidate.fold_index === fold.fold_index);
    if (position < 0) return null;

    const newerCount = folds.length - position - 1;
    return {
      position: position + 1,
      total: folds.length,
      newerCount,
      label: newerCount === 0
        ? 'Most recent fold'
        : position === 0
          ? 'Oldest fold'
          : `${newerCount} newer fold${newerCount === 1 ? '' : 's'} follow`,
      newerFoldCopy: newerCount === 0
        ? 'No newer folds follow this one in the receipt.'
        : `${newerCount} newer fold${newerCount === 1 ? ' follows' : 's follow'} this one in the receipt.`,
    };
  });

  onVisibleChange(visible: boolean): void {
    if (!visible) this.closed.emit();
  }
}
