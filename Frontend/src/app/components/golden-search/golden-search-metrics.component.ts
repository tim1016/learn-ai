import { DecimalPipe, PercentPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import type { Metrics } from './golden-search.types';

/**
 * One evaluation's engine statistics as Python reported them (#2696):
 * return, drawdown and win rate are fractions rendered as percentages; an
 * undefined statistic reads "—", never zero; a failed run shows its error.
 */
@Component({
  selector: 'app-golden-search-metrics',
  imports: [DecimalPipe, PercentPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-metrics.component.html',
  styleUrl: './golden-search-metrics.component.scss',
})
export class GoldenSearchMetricsComponent {
  readonly metrics = input<Metrics | null>(null);
  readonly compact = input(false);
}
