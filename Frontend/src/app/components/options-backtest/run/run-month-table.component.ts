import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { TableModule } from 'primeng/table';

import { AuthoredUsdPipe } from '../../../shared/pipes/authored-usd.pipe';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import type { MonthRow } from '../options-backtest.types';
import { monthLabel } from '../run-words';

/**
 * One row for each New York calendar month the run covered. A month is named
 * by the year and month the server counts it by; the columns of whole
 * numbers sort, the dollar columns do not.
 */
@Component({
  selector: 'app-options-backtest-run-month-table',
  imports: [TableModule, ReceiptLabelPipe, AuthoredUsdPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './run-month-table.component.html',
  styleUrl: './run-tables.scss',
})
export class RunMonthTableComponent {
  readonly rows = input.required<MonthRow[]>();
  protected readonly labelled = computed(() => this.rows().map((row) => ({ ...row, label: monthLabel(row.year, row.month) })));
}
