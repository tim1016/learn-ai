import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../shared/timestamp';
import { GoldenSearchMetricsComponent } from './golden-search-metrics.component';
import type { ValidationFold } from './golden-search.types';

export interface FoldRow {
  readonly fold: ValidationFold;
  /** The fold winner's settings that differ from the frozen starting point, or null when no winner was chosen. */
  readonly changes: string | null;
}

/**
 * Every fold of the test over time (#2696), failures included: its training
 * and test months (ET dates, exclusive ends shown as the last day), the
 * training winner's change from the starting point, the training and test
 * results, and the frozen incumbent on the same test months.
 */
@Component({
  selector: 'app-golden-search-fold-table',
  imports: [GoldenSearchMetricsComponent, ReceiptLabelPipe, TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-fold-table.component.html',
  styleUrl: './golden-search-test-step.component.scss',
})
export class GoldenSearchFoldTableComponent {
  readonly rows = input.required<readonly FoldRow[]>();
}
