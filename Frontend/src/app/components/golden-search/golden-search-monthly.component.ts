import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { TimestampDisplayComponent } from '../../shared/timestamp';
import { signedPercentText, signedUsdText } from './golden-search-display';
import type { MonthlyResult } from './golden-search.types';

interface MonthRow {
  readonly month_start_ms: number;
  readonly returnText: string;
  readonly profitText: string;
  readonly trades: number;
  readonly loss: boolean;
  /** Bar length as a share of the largest month, in percent (display scale only). */
  readonly barPercent: number;
}

/**
 * Performance by month (#2696): each ET calendar month's net result as the
 * server computed it, a bar per month for the shape, and the same values in
 * the table the bars sit in. It describes changing performance; it claims no
 * alpha model or decay.
 */
@Component({
  selector: 'app-golden-search-monthly',
  imports: [TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-monthly.component.html',
  styleUrl: './golden-search-evidence-table.scss',
})
export class GoldenSearchMonthlyComponent {
  readonly monthly = input.required<readonly MonthlyResult[]>();
  readonly candidateLabel = input.required<string>();

  protected readonly rows = computed<MonthRow[]>(() => {
    const months = this.monthly();
    const largest = Math.max(0, ...months.map((m) => Math.abs(m.return_fraction)));
    return months.map((m) => ({
      month_start_ms: m.month_start_ms,
      returnText: signedPercentText(m.return_fraction),
      profitText: signedUsdText(m.net_profit),
      trades: m.trades,
      loss: m.return_fraction < 0,
      barPercent: largest > 0 ? (Math.abs(m.return_fraction) / largest) * 100 : 0,
    }));
  });
}
