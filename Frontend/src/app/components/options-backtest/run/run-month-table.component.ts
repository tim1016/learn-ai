import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { TableModule } from 'primeng/table';

import { AuthoredUsdPipe, formatAuthoredUsd } from '../../../shared/pipes/authored-usd.pipe';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import type { MonthRow } from '../options-backtest.types';
import { monthLabel } from '../run-words';
import { heatStrengths, isLoss } from './run-chart-scale';

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
  protected readonly heat = computed(() => {
    const rows = this.labelled();
    const strengths = heatStrengths(rows.map((row) => row.net_profit_usd));
    return rows.map((row, index) => ({
      ...row,
      loss: isLoss(row.net_profit_usd),
      strength: strengths[index],
      title: `${row.label}: ${formatAuthoredUsd(row.net_profit_usd)} net profit across ${row.sessions_traded} traded sessions`,
    }));
  });
}
